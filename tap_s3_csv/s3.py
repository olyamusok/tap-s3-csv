import itertools
import re
import io
import json
import gzip
import backoff
import boto3
import singer
import tempfile
import pathlib
import os
import pyarrow.parquet as pq
import pytz

from dateutil.parser import parse
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    CredentialResolver,
    DeferredRefreshableCredentials,
    JSONFileCache
)
from botocore.exceptions import ClientError
from botocore.session import Session
from singer_encodings import (
    compression,
    csv
)

from tap_s3_csv import (
    utils,
    conversion
)

LOGGER = singer.get_logger()

SDC_SOURCE_BUCKET_COLUMN = "_sdc_source_bucket"
SDC_SOURCE_FILE_COLUMN = "_sdc_source_file"
SDC_SOURCE_LINENO_COLUMN = "_sdc_source_lineno"
SDC_EXTRA_COLUMN = "_sdc_extra"
skipped_files_count = 0

def retry_pattern():
    return backoff.on_exception(backoff.expo,
                                ClientError,
                                max_tries=5,
                                on_backoff=log_backoff_attempt,
                                factor=10)


def log_backoff_attempt(details):
    LOGGER.info("Error detected communicating with Amazon, triggering backoff: %d try", details.get("tries"))


class AssumeRoleProvider():
    METHOD = 'assume-role'

    def __init__(self, fetcher):
        self._fetcher = fetcher

    def load(self):
        return DeferredRefreshableCredentials(
            self._fetcher.fetch_credentials,
            self.METHOD
        )


@retry_pattern()
def setup_aws_client(config):
    key = config.get('aws_access_key_id', os.environ.get("aws_access_key_id"))
    secret = config.get('aws_secret_access_key', os.environ.get("aws_secret_access_key"))

    return boto3.session.Session(aws_access_key_id=key, aws_secret_access_key=secret)


def get_sampled_schema_for_table(config, table_spec):
    LOGGER.info('Sampling records to determine table schema.')

    s3_files_gen = get_input_files_for_table(
        config, 
        table_spec,
        modified_since=parse(config.get('start_date')).replace(tzinfo=pytz.UTC),
        modified_until=config.get('end_date'))

    samples = [sample for sample in sample_files(config, table_spec, s3_files_gen)]

    if skipped_files_count:
        LOGGER.warning("%s files got skipped during the last sampling.",skipped_files_count)

    if not samples:
        #Return empty properties for accept everything from data if no samples found
        return {
            'type': 'object',
            'properties': {}
        }

    metadata_schema = {
        SDC_SOURCE_BUCKET_COLUMN: {'type': 'string'},
        SDC_SOURCE_FILE_COLUMN: {'type': 'string'},
        SDC_SOURCE_LINENO_COLUMN: {'type': 'integer'},
        SDC_EXTRA_COLUMN: {'type': 'array', 'items': {
            'anyOf': [{'type': 'object', 'properties': {}}, {'type': 'string'}]}}
    }

    data_schema = conversion.generate_schema(samples, table_spec)

    return {
        'type': 'object',
        'properties': merge_dicts(data_schema, metadata_schema)
    }

def merge_dicts(first, second):
    to_return = first.copy()

    for key in second:
        if key in first:
            if isinstance(first[key], dict) and isinstance(second[key], dict):
                to_return[key] = merge_dicts(first[key], second[key])
            else:
                to_return[key] = second[key]

        else:
            to_return[key] = second[key]

    return to_return


def get_records_for_csv(s3_path, sample_rate, iterator):

    current_row = 0
    sampled_row_count = 0

    for row in iterator:

        # Skipping the empty line of CSV.
        if len(row) == 0:
            current_row += 1
            continue

        if (current_row % sample_rate) == 0:
            if row.get(csv.SDC_EXTRA_COLUMN):
                row.pop(csv.SDC_EXTRA_COLUMN)
            sampled_row_count += 1
            if (sampled_row_count % 200) == 0:
                LOGGER.info("Sampled %s rows from %s",
                            sampled_row_count, s3_path)
            yield row

        current_row += 1

    LOGGER.info("Sampled %s rows from %s", sampled_row_count, s3_path)


def get_records_for_jsonl(s3_path, sample_rate, iterator):

    current_row = 0
    sampled_row_count = 0

    for row in iterator:

        if (current_row % sample_rate) == 0:
            decoded_row = row.decode('utf-8')
            if decoded_row.strip():
                row = json.loads(decoded_row)
                # Skipping the empty json.
                if len(row) == 0:
                    current_row += 1
                    continue
            else:
                current_row += 1
                continue
            sampled_row_count += 1
            if (sampled_row_count % 200) == 0:
                LOGGER.info("Sampled %s rows from %s",
                            sampled_row_count, s3_path)
            yield row

        current_row += 1

    LOGGER.info("Sampled %s rows from %s", sampled_row_count, s3_path)


def get_records_for_parquet(s3_bucket, s3_path, sample_rate, config):

    local_path = os.path.join(tempfile.gettempdir(), s3_path)
    pathlib.Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    if os.path.isfile(local_path):
        LOGGER.info(f"Skipping download, file exists: {local_path}")
    else:
        LOGGER.info(f"Downloading {s3_path} to {local_path}")
        session = setup_aws_client(config)
        session.resource("s3").Bucket(s3_bucket).download_file(s3_path, local_path)

    parquet_file = pq.ParquetFile(local_path)

    current_row = 0
    sampled_row_count = 0

    for i in range(parquet_file.num_row_groups):
        table = parquet_file.read_row_group(i)
        for batch in table.to_batches():
            for row in zip(*batch.columns):
                if (current_row % sample_rate) == 0:                                
                    current_row += 1
                    sampled_row_count += 1
                    yield {
                        table.column_names[i]: val.as_py()
                        for i, val in enumerate(row, start=0)
                    }

    LOGGER.info("Sampled %s rows from %s", sampled_row_count, s3_path)


def check_key_properties_and_date_overrides_for_jsonl_file(table_spec, jsonl_sample_records, s3_path):

    rows = 0
    all_keys = set()
    for record in jsonl_sample_records:
        rows += 1
        keys = record.keys()
        all_keys.update(keys)
        if rows > 5000:
            break

    if table_spec.get('key_properties'):
        key_properties = set(table_spec['key_properties'])
        if not key_properties.issubset(all_keys):
            raise Exception('JSONL/parquet file "{}" is missing required key_properties key: {}'
                            .format(s3_path, key_properties - all_keys))

    if table_spec.get('date_overrides'):
        date_overrides = set(table_spec['date_overrides'])
        if not date_overrides.issubset(all_keys):
            raise Exception('JSONL/parquet file "{}" is missing date_overrides key: {}'
                            .format(s3_path, date_overrides - all_keys))

#pylint: disable=global-statement
def sampling_gz_file(table_spec, s3_path, file_handle, sample_rate, config):
    global skipped_files_count
    if s3_path.endswith(".tar.gz"):
        LOGGER.warning('Skipping "%s" file as .tar.gz extension is not supported',s3_path)
        skipped_files_count = skipped_files_count + 1
        return []

    file_bytes = file_handle.read()
    gz_file_obj = gzip.GzipFile(fileobj=io.BytesIO(file_bytes))

    try:
        gz_file_name = utils.get_file_name_from_gzfile(fileobj=io.BytesIO(file_bytes))
    except AttributeError as err:
        # If a file is compressed using gzip command with --no-name attribute,
        # It will not return the file name and timestamp. Hence we will skip such files.
        # We also seen this issue occur when tar is used to compress the file
        LOGGER.warning('Skipping "%s" file as we did not get the original file name',s3_path)
        skipped_files_count = skipped_files_count + 1
        return []

    if gz_file_name:
        if gz_file_name.endswith(".gz"):
            LOGGER.warning('Skipping "%s" file as it contains nested compression.',s3_path)
            skipped_files_count = skipped_files_count + 1
            return []

        gz_file_extension = gz_file_name.split(".")[-1].lower()
        return sample_file(table_spec, s3_path + "/" + gz_file_name, io.BytesIO(gz_file_obj.read()), sample_rate, gz_file_extension, config)

    raise Exception('"{}" file has some error(s)'.format(s3_path))

def peek(iterable):
    try:
        first = next(iterable)
    except StopIteration:
        return None
    return first, itertools.chain([first], iterable)

#pylint: disable=global-statement
def sample_file(table_spec, s3_bucket, s3_path, file_handle, sample_rate, extension, config):
    global skipped_files_count

    # Check whether file is without extension or not
    if not extension or s3_path.lower() == extension:
        LOGGER.warning('"%s" without extension will not be sampled.',s3_path)
        skipped_files_count = skipped_files_count + 1
        return []
    if extension in ["csv", "txt"]:
        # If file object read from s3 bucket file else use extracted file object from zip or gz
        file_handle = file_handle._raw_stream if hasattr(file_handle, "_raw_stream") else file_handle #pylint:disable=protected-access
        iterator = csv.get_row_iterator(file_handle, table_spec, None, True)
        csv_records = []
        if iterator:
            csv_records = get_records_for_csv(s3_path, sample_rate, iterator)
        else:
            LOGGER.warning('Skipping "%s" file as it is empty',s3_path)
            skipped_files_count = skipped_files_count + 1
        return csv_records
    if extension == "gz":
        return sampling_gz_file(table_spec, s3_path, file_handle, sample_rate, config)
    if extension == "jsonl":
        # If file object read from s3 bucket file else use extracted file object from zip or gz

        file_handle = file_handle._raw_stream if hasattr(file_handle, "_raw_stream") else file_handle
        records = get_records_for_jsonl(s3_path, sample_rate, file_handle)
        check_jsonl_sample_records, records = itertools.tee(records)
        
        result = peek(check_jsonl_sample_records)
        if result is None:
            LOGGER.warning('Skipping "%s" file as it is empty', s3_path)
            skipped_files_count = skipped_files_count + 1
            return []
        else:
            check_jsonl_sample_records = result[1]
        check_key_properties_and_date_overrides_for_jsonl_file(table_spec, check_jsonl_sample_records, s3_path)
        return records
    if extension == "parquet":
        records = get_records_for_parquet(s3_bucket, s3_path, sample_rate, config)
        check_jsonl_sample_records, records = itertools.tee(records)
        
        result = peek(check_jsonl_sample_records)
        if result is None:
            LOGGER.warning('Skipping "%s" file as it is empty', s3_path)
            skipped_files_count = skipped_files_count + 1
            return []
        else:
            check_jsonl_sample_records = result[1]
        check_key_properties_and_date_overrides_for_jsonl_file(table_spec, jsonl_sample_records, s3_path)
        
        return records
    if extension == "zip":
        LOGGER.warning('Skipping "%s" file as it contains nested compression.',s3_path)
        skipped_files_count = skipped_files_count + 1
        return []
    LOGGER.warning('"%s" having the ".%s" extension will not be sampled.',s3_path,extension)
    skipped_files_count = skipped_files_count + 1
    return []

#pylint: disable=global-statement
def get_files_to_sample(config, s3_files, max_files):
    """
    Returns the list of files for sampling, it checks the s3_files whether any zip or gz file exists or not
    if exists then extract if and append in the list of files

    Args:
        config dict(): Configuration
        s3_files list(): List of S3 Bucket files
    Returns:
        list(dict()) : List of Files for sampling
             |_ s3_path str(): S3 Bucket File path
             |_ file_handle StreamingBody(): file object
             |_ type str(): Type of file which is used for extracted file
             |_ extension str(): extension of file (for normal files only)
    """
    global skipped_files_count
    sampled_files = []

    OTHER_FILES = ["csv","gz","jsonl","txt","parquet"]

    for s3_file in s3_files:
        file_key = s3_file.get('key')

        if len(sampled_files) >= max_files:
            break

        if file_key:
            file_name = file_key.split("/").pop()
            extension = file_name.split(".").pop().lower()
            file_handle = get_file_handle(config, file_key)

            # Check whether file is without extension or not
            if not extension or file_name.lower() == extension:
                LOGGER.warning('"%s" without extension will not be sampled.',file_key)
                skipped_files_count = skipped_files_count + 1
            elif file_key.endswith(".tar.gz"):
                LOGGER.warning('Skipping "%s" file as .tar.gz extension is not supported', file_key)
                skipped_files_count = skipped_files_count + 1
            elif extension == "zip":
                files = compression.infer(io.BytesIO(file_handle.read()), file_name)

                # Add only those extracted files which are supported by tap
                # Prepare dictionary contains the zip file name, type i.e. unzipped and file object of extracted file
                sampled_files.extend([{ "type" : "unzipped", "s3_path" : file_key, "file_handle" : de_file } for de_file in files if de_file.name.split(".")[-1].lower() in OTHER_FILES and not de_file.name.endswith(".tar.gz") ])
            elif extension in OTHER_FILES:
                # Prepare dictionary contains the s3 file path, extension of file and file object
                sampled_files.append({ "s3_path" : file_key , "file_handle" : file_handle, "extension" : extension })
            else:
                LOGGER.warning('"%s" having the ".%s" extension will not be sampled.',file_key,extension)
                skipped_files_count = skipped_files_count + 1

    return sampled_files


# pylint: disable=too-many-arguments,global-statement
def sample_files(config, table_spec, s3_files,
                 sample_rate=5, max_records=1000, max_files=5):
    global skipped_files_count
    max_files = config.get("max_sample_files", max_files)
    LOGGER.info("Sampling files (max files: %s)", max_files)

    for s3_file in itertools.islice(get_files_to_sample(config, s3_files, max_files), max_files):

        s3_bucket = config['bucket']
        s3_path = s3_file.get("s3_path","")
        file_handle = s3_file.get("file_handle")
        file_type = s3_file.get("type")
        extension = s3_file.get("extension")

        # Check whether the file is extracted from zip file.
        if file_type and file_type == "unzipped":
            # Append the extracted file name with zip file.
            s3_path += "/" + file_handle.name
            extension = file_handle.name.split(".")[-1].lower()

        LOGGER.info('Sampling %s (max records: %s, sample rate: %s)',
                    s3_path,
                    max_records,
                    sample_rate)
        try:
            yield from itertools.islice(sample_file(table_spec, s3_bucket, s3_path, file_handle, sample_rate, extension, config), max_records)
        except (UnicodeDecodeError,json.decoder.JSONDecodeError):
            # UnicodeDecodeError will be raised if non csv file parsed to csv parser
            # JSONDecodeError will be reaised if non JSONL file parsed to JSON parser
            # Handled both error and skipping file with wrong extension.
            LOGGER.warn("Skipping %s file as parsing failed. Verify an extension of the file.",s3_path)
            skipped_files_count = skipped_files_count + 1

#pylint: disable=global-statement
def get_input_files_for_table(config, table_spec, modified_since=None, modified_until=None):
    global skipped_files_count
    bucket = config['bucket']

    to_return = []

    pattern = table_spec['search_pattern']
    try:
        matcher = re.compile(pattern)
    except re.error as e:
        raise ValueError(
            ("search_pattern for table `{}` is not a valid regular "
             "expression. See "
             "https://docs.python.org/3.5/library/re.html#regular-expression-syntax").format(table_spec['table_name']),
            pattern) from e

    LOGGER.info('Checking bucket "%s" for keys matching "%s"', bucket, pattern)
    LOGGER.info('Window period: since %s until %s',modified_since,modified_until)

    matched_files_count = 0
    unmatched_files_count = 0
    max_files_before_log = 30000
    for s3_object in list_files_in_bucket(bucket, table_spec.get('search_prefix'), config=config):
        key = s3_object['Key']
        last_modified = s3_object['LastModified']

        if s3_object['Size'] == 0:
            LOGGER.warning('Skipping matched file "%s" as it is empty', key)
            skipped_files_count = skipped_files_count + 1
            unmatched_files_count += 1
            continue

        if matcher.search(key):
            matched_files_count += 1
            if modified_since is None or modified_since < last_modified:
                if modified_until is None or last_modified.replace(tzinfo=pytz.UTC) < parse(modified_until).replace(tzinfo=pytz.UTC):
                    LOGGER.info('Will download key "%s" as it was last modified %s',key,last_modified)
                    yield {'key': key, 'last_modified': last_modified}
        else:
            unmatched_files_count += 1

        if (unmatched_files_count + matched_files_count) % max_files_before_log == 0:
            # Are we skipping greater than 50% of the files?
            if (unmatched_files_count / (matched_files_count + unmatched_files_count)) > 0.5:
                LOGGER.warn(("Found %s matching files and %s non-matching files. "
                             "You should consider adding a `search_prefix` to the config "
                             "or removing non-matching files from the bucket."),
                            matched_files_count, unmatched_files_count)
            else:
                LOGGER.info("Found %s matching files and %s non-matching files",
                            matched_files_count, unmatched_files_count)

    if matched_files_count == 0:
        raise Exception("No files found matching pattern {}".format(pattern))


@retry_pattern()
def list_files_in_bucket(bucket, search_prefix=None, config=None):
    s3_client = setup_aws_client(config).client('s3')

    s3_object_count = 0

    max_results = 1000
    args = {
        'Bucket': bucket,
        'MaxKeys': max_results,
    }

    if search_prefix is not None:
        args['Prefix'] = search_prefix

    paginator = s3_client.get_paginator('list_objects_v2')
    pages = 0
    for page in paginator.paginate(**args):
        pages += 1
        LOGGER.debug("On page %s", pages)
        s3_object_count += len(page['Contents'])
        yield from page['Contents']

    if s3_object_count > 0:
        LOGGER.info("Found %s files.", s3_object_count)
    else:
        LOGGER.warning('Found no files for bucket "%s" that match prefix "%s"', bucket, search_prefix)


@retry_pattern()
def get_file_handle(config, s3_path):
    bucket = config['bucket']
    s3_client = setup_aws_client(config).resource('s3')

    s3_bucket = s3_client.Bucket(bucket)
    s3_object = s3_bucket.Object(s3_path)
    return s3_object.get()['Body']
