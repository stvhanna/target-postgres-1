import io
import re
import csv
import uuid
import json
from functools import partial
from copy import deepcopy

import arrow
from psycopg2 import sql

from target_postgres import json_schema
from target_postgres.singer_stream import (
    SINGER_RECEIVED_AT,
    SINGER_BATCHED_AT,
    SINGER_SEQUENCE,
    SINGER_TABLE_VERSION,
    SINGER_PK,
    SINGER_SOURCE_PK_PREFIX,
    SINGER_LEVEL
)

RESERVED_NULL_DEFAULT = 'NULL'


class PostgresError(Exception):
    """
    Raise this when there is an error with regards to Postgres streaming
    """

class TransformStream():
    def __init__(self, fun):
        self.fun = fun

    def read(self, *args, **kwargs):
        return self.fun()

class PostgresTarget():
    SEPARATOR = '__'

    def __init__(self, connection, logger, *args, postgres_schema='public', **kwargs):
        self.conn = connection
        self.logger = logger
        self.postgres_schema = postgres_schema

    def write_batch(self, stream_buffer):
        if stream_buffer.count == 0:
            return

        with self.conn.cursor() as cur:
            try:
                cur.execute('BEGIN;')

                processed_records = map(partial(self.process_record_message,
                                                stream_buffer.use_uuid_pk,
                                                self.get_postgres_datetime()),
                                        stream_buffer.peek_buffer())
                versions = set()
                max_version = None
                records_all_versions = []
                for record in processed_records:
                    record_version = record.get(SINGER_TABLE_VERSION)
                    if record_version is not None and \
                       (max_version is None or record_version > max_version):
                        max_version = record_version
                    versions.add(record_version)
                    records_all_versions.append(record)

                current_table_schema = self.get_schema(cur,
                                                       self.postgres_schema,
                                                       stream_buffer.stream)

                current_table_version = None

                if current_table_schema:
                    current_table_version = current_table_schema.get('version', None)

                    if set(stream_buffer.key_properties) \
                            != set(current_table_schema.get('key_properties')):
                        raise PostgresError(
                            '`key_properties` change detected. Existing values are: {}. Streamed values are: {}'.format(
                                current_table_schema.get('key_properties'),
                                stream_buffer.key_properties
                            ))

                if max_version is not None:
                    target_table_version = max_version
                else:
                    target_table_version = None

                if current_table_version is not None and \
                        min(versions) < current_table_version:
                    self.logger.warning('{} - Records from an earlier table vesion detected.'
                                        .format(stream_buffer.stream))
                if len(versions) > 1:
                    self.logger.warning('{} - Multiple table versions in stream, only using the latest.'
                                        .format(stream_buffer.stream))

                if current_table_version is not None and \
                   target_table_version > current_table_version:
                    root_table_name = stream_buffer.stream + self.SEPARATOR + str(target_table_version)
                else:
                    root_table_name = stream_buffer.stream

                if target_table_version is not None:
                    records = filter(lambda x: x.get(SINGER_TABLE_VERSION) == target_table_version,
                                     records_all_versions)
                else:
                    records = records_all_versions

                root_table_schema = json_schema.simplify(stream_buffer.schema)

                ## Add singer columns to root table
                self.add_singer_columns(root_table_schema, stream_buffer.key_properties)

                subtables = {}
                key_prop_schemas = {}
                for key in stream_buffer.key_properties:
                    if current_table_schema \
                            and json_schema.get_type(current_table_schema['schema']['properties'][key]) \
                            != json_schema.get_type(root_table_schema['properties'][key]):
                        raise PostgresError(
                            ('`key_properties` type change detected for "{}". ' +
                            'Existing values are: {}. ' +
                            'Streamed values are: {}').format(
                                key,
                                json_schema.get_type(current_table_schema['schema']['properties'][key]),
                                json_schema.get_type(root_table_schema['properties'][key])
                            ))

                    key_prop_schemas[key] = root_table_schema['properties'][key]

                self.denest_schema(root_table_name, root_table_schema, key_prop_schemas, subtables)

                root_temp_table_name = self.upsert_table_schema(cur,
                                                                root_table_name,
                                                                root_table_schema,
                                                                stream_buffer.key_properties,
                                                                target_table_version)

                nested_upsert_tables = []
                for table_name, subtable_json_schema in subtables.items():
                    temp_table_name = self.upsert_table_schema(cur,
                                                               table_name,
                                                               subtable_json_schema,
                                                               None,
                                                               None)
                    nested_upsert_tables.append({
                        'table_name': table_name,
                        'json_schema': subtable_json_schema,
                        'temp_table_name': temp_table_name
                    })

                records_map = {}
                self.denest_records(root_table_name, records, records_map, stream_buffer.key_properties)
                self.persist_rows(cur,
                                  root_table_name,
                                  root_temp_table_name,
                                  root_table_schema,
                                  stream_buffer.key_properties,
                                  records_map[root_table_name])
                for nested_upsert_table in nested_upsert_tables:
                    key_properties = []
                    for key in stream_buffer.key_properties:
                        key_properties.append(SINGER_SOURCE_PK_PREFIX + key)
                    self.persist_rows(cur,
                                      nested_upsert_table['table_name'],
                                      nested_upsert_table['temp_table_name'],
                                      nested_upsert_table['json_schema'],
                                      key_properties,
                                      records_map[nested_upsert_table['table_name']])

                cur.execute('COMMIT;')
            except Exception as ex:
                cur.execute('ROLLBACK;')
                message = 'Exception writing records'
                self.logger.exception(message)
                raise PostgresError(message, ex)

        stream_buffer.flush_buffer()

    def activate_version(self, stream_buffer, version):
        with self.conn.cursor() as cur:
            try:
                cur.execute('BEGIN;')

                table_metadata = self.get_table_metadata(cur,
                                                         self.postgres_schema,
                                                         stream_buffer.stream)

                if not table_metadata:
                    self.logger.error('{} - Table for stream does not exist'.format(
                        stream_buffer.stream))
                elif table_metadata.get('version') == version:
                    self.logger.warning('{} - Table version {} already active'.format(
                        stream_buffer.stream,
                        version))
                else:
                    versioned_root_table = stream_buffer.stream + self.SEPARATOR + str(version)

                    cur.execute(
                        sql.SQL('''
                        SELECT tablename FROM pg_tables
                        WHERE schemaname = {} AND tablename like {};
                        ''').format(
                            sql.Literal(self.postgres_schema),
                            sql.Literal(versioned_root_table + '%')))

                    for versioned_table_name in map(lambda x: x[0], cur.fetchall()):
                        table_name = stream_buffer.stream + versioned_table_name[len(versioned_root_table):]
                        cur.execute(
                            sql.SQL('''
                            ALTER TABLE {table_schema}.{stream_table} RENAME TO {stream_table_old};
                            ALTER TABLE {table_schema}.{version_table} RENAME TO {stream_table};
                            DROP TABLE {table_schema}.{stream_table_old};
                            COMMIT;''').format(
                                table_schema=sql.Identifier(self.postgres_schema),
                                stream_table_old=sql.Identifier(table_name +
                                                                self.SEPARATOR +
                                                                'old'),
                                stream_table=sql.Identifier(table_name),
                                version_table=sql.Identifier(versioned_table_name)))
            except Exception as ex:
                cur.execute('ROLLBACK;')
                message = '{} - Exception activating table version {}'.format(
                    stream_buffer.stream,
                    version)
                self.logger.exception(message)
                raise PostgresError(message, ex)

    def add_singer_columns(self, schema, key_properties):
        properties = schema['properties']

        if SINGER_RECEIVED_AT not in properties:
            properties[SINGER_RECEIVED_AT] = {
                'type': ['null', 'string'],
                'format': 'date-time'
            }

        if SINGER_SEQUENCE not in properties:
            properties[SINGER_SEQUENCE] = {
                'type': ['null', 'integer']
            }

        if SINGER_TABLE_VERSION not in properties:
            properties[SINGER_TABLE_VERSION] = {
                'type': ['null', 'integer']
            }

        if SINGER_BATCHED_AT not in properties:
            properties[SINGER_BATCHED_AT] = {
                'type': ['null', 'string'],
                'format': 'date-time'
            }

        if len(key_properties) == 0:
            properties[SINGER_PK] = {
                'type': ['string']
            }

    def process_record_message(self, use_uuid_pk, batched_at, record_message):
        record = record_message['record']

        if 'version' in record_message:
            record[SINGER_TABLE_VERSION] = record_message['version']

        if 'time_extracted' in record_message and record.get(SINGER_RECEIVED_AT) is None:
            record[SINGER_RECEIVED_AT] = record_message['time_extracted']

        if use_uuid_pk and record.get(SINGER_PK) is None:
            record[SINGER_PK] = uuid.uuid4()

        record[SINGER_BATCHED_AT] = batched_at

        if 'sequence' in record_message:
            record[SINGER_SEQUENCE] = record_message['sequence']
        else:
            record[SINGER_SEQUENCE] = arrow.get().timestamp

        return record

    def denest_schema_helper(self,
                             table_name,
                             table_json_schema,
                             not_null,
                             top_level_schema,
                             current_path,
                             key_prop_schemas,
                             subtables,
                             level):
        for prop, item_json_schema in table_json_schema['properties'].items():
            next_path = current_path + self.SEPARATOR + prop
            if json_schema.is_object(item_json_schema):
                self.denest_schema_helper(table_name,
                                          item_json_schema,
                                          not_null,
                                          top_level_schema,
                                          next_path,
                                          key_prop_schemas,
                                          subtables,
                                          level)
            elif json_schema.is_iterable(item_json_schema):
                self.create_subtable(table_name + self.SEPARATOR + prop,
                                     item_json_schema,
                                     key_prop_schemas,
                                     subtables,
                                     level + 1)
            else:
                if not_null and json_schema.is_nullable(item_json_schema):
                    item_json_schema['type'].remove('null')
                elif not json_schema.is_nullable(item_json_schema):
                    item_json_schema['type'].append('null')
                top_level_schema[next_path] = item_json_schema

    def create_subtable(self, table_name, table_json_schema, key_prop_schemas, subtables, level):
        if json_schema.is_object(table_json_schema['items']):
            new_properties = table_json_schema['items']['properties']
        else:
            new_properties = {'value': table_json_schema['items']}

        for pk, item_json_schema in key_prop_schemas.items():
            new_properties[SINGER_SOURCE_PK_PREFIX + pk] = item_json_schema

        new_properties[SINGER_SEQUENCE] = {
            'type': ['null', 'integer']
        }

        for i in range(0, level + 1):
            new_properties[SINGER_LEVEL.format(i)] = {
                'type': ['integer']
            }

        new_schema = {'type': ['object'], 'properties': new_properties}

        self.denest_schema(table_name, new_schema, key_prop_schemas, subtables, level=level)

        subtables[table_name] = new_schema

    def denest_schema(self, table_name, table_json_schema, key_prop_schemas, subtables, current_path=None, level=-1):
        new_properties = {}
        for prop, item_json_schema in table_json_schema['properties'].items():
            if current_path:
                next_path = current_path + self.SEPARATOR + prop
            else:
                next_path = prop

            if json_schema.is_object(item_json_schema):
                not_null = 'null' not in item_json_schema['type']
                self.denest_schema_helper(table_name + self.SEPARATOR + next_path,
                                          item_json_schema,
                                          not_null,
                                          new_properties,
                                          next_path,
                                          key_prop_schemas,
                                          subtables,
                                          level)
            elif json_schema.is_iterable(item_json_schema):
                self.create_subtable(table_name + self.SEPARATOR + next_path,
                                     item_json_schema,
                                     key_prop_schemas,
                                     subtables,
                                     level + 1)
            else:
                new_properties[prop] = item_json_schema
        table_json_schema['properties'] = new_properties

    def denest_subrecord(self,
                         table_name,
                         current_path,
                         parent_record,
                         record,
                         records_map,
                         key_properties,
                         pk_fks,
                         level):
        for prop, value in record.items():
            next_path = current_path + self.SEPARATOR + prop
            if isinstance(value, dict):
                self.denest_subrecord(table_name, next_path, parent_record, value, pk_fks, level)
            elif isinstance(value, list):
                self.denest_records(table_name + self.SEPARATOR + next_path,
                                    value,
                                    records_map,
                                    key_properties,
                                    pk_fks=pk_fks,
                                    level=level + 1)
            else:
                parent_record[next_path] = value

    def denest_record(self, table_name, current_path, record, records_map, key_properties, pk_fks, level):
        denested_record = {}
        for prop, value in record.items():
            if current_path:
                next_path = current_path + self.SEPARATOR + prop
            else:
                next_path = prop

            if isinstance(value, dict):
                self.denest_subrecord(table_name,
                                      next_path,
                                      denested_record,
                                      value,
                                      records_map,
                                      key_properties,
                                      pk_fks,
                                      level)
            elif isinstance(value, list):
                self.denest_records(table_name + self.SEPARATOR + next_path,
                                    value,
                                    records_map,
                                    key_properties,
                                    pk_fks=pk_fks,
                                    level=level + 1)
            elif value is None: ## nulls mess up nested objects
                continue
            else:
                denested_record[next_path] = value

        if table_name not in records_map:
            records_map[table_name] = []
        records_map[table_name].append(denested_record)

    def denest_records(self, table_name, records, records_map, key_properties, pk_fks=None, level=-1):
        row_index = 0
        for record in records:
            if pk_fks:
                record_pk_fks = pk_fks.copy()
                record_pk_fks[SINGER_LEVEL.format(level)] = row_index
                for key, value in record_pk_fks.items():
                    record[key] = value
                row_index += 1
            else: ## top level
                record_pk_fks = {}
                for key in key_properties:
                    record_pk_fks[SINGER_SOURCE_PK_PREFIX + key] = record[key]
                if SINGER_SEQUENCE in record:
                    record_pk_fks[SINGER_SEQUENCE] = record[SINGER_SEQUENCE]
            self.denest_record(table_name, None, record, records_map, key_properties, record_pk_fks, level)

    def upsert_table_schema(self, cur, table_name, schema, key_properties, table_version):
        existing_table_schema = self.get_schema(cur, self.postgres_schema, table_name)

        if existing_table_schema is not None:
            self.merge_put_schemas(cur,
                                   self.postgres_schema,
                                   table_name,
                                   existing_table_schema,
                                   schema)
        else:
            self.create_table(cur,
                              self.postgres_schema,
                              table_name,
                              schema,
                              key_properties,
                              table_version)

        target_table_name = self.get_temp_table_name(table_name)

        self.create_table(cur,
                          self.postgres_schema,
                          target_table_name,
                          self.get_schema(cur, self.postgres_schema, table_name).get('schema'),
                          key_properties,
                          table_version)

        return target_table_name

    def get_update_sql(self, target_table_name, temp_table_name, key_properties, subkeys):
        full_table_name = sql.SQL('{}.{}').format(
            sql.Identifier(self.postgres_schema),
            sql.Identifier(target_table_name))
        full_temp_table_name = sql.SQL('{}.{}').format(
            sql.Identifier(self.postgres_schema),
            sql.Identifier(temp_table_name))

        pk_temp_select_list = []
        pk_where_list = []
        pk_null_list = []
        cxt_where_list = []
        for pk in key_properties:
            pk_identifier = sql.Identifier(pk)
            pk_temp_select_list.append(sql.SQL('{}.{}').format(full_temp_table_name,
                                                               pk_identifier))

            pk_where_list.append(
                sql.SQL('{table}.{pk} = {temp_table}.{pk}').format(
                    table=full_table_name,
                    temp_table=full_temp_table_name,
                    pk=pk_identifier))

            pk_null_list.append(
                sql.SQL('{table}.{pk} IS NULL').format(
                    table=full_table_name,
                    pk=pk_identifier))

            cxt_where_list.append(
                sql.SQL('{table}.{pk} = "pks".{pk}').format(
                    table=full_table_name,
                    pk=pk_identifier))
        pk_temp_select = sql.SQL(', ').join(pk_temp_select_list)
        pk_where = sql.SQL(' AND ').join(pk_where_list)
        pk_null = sql.SQL(' AND ').join(pk_null_list)
        cxt_where = sql.SQL(' AND ').join(cxt_where_list)

        sequence_join = sql.SQL(' AND {}.{} >= {}.{}').format(
            full_temp_table_name,
            sql.Identifier(SINGER_SEQUENCE),
            full_table_name,
            sql.Identifier(SINGER_SEQUENCE))

        distinct_order_by = sql.SQL(' ORDER BY {}, {}.{} DESC').format(
            pk_temp_select,
            full_temp_table_name,
            sql.Identifier(SINGER_SEQUENCE))

        if len(subkeys) > 0:
            pk_temp_subkey_select_list = []
            for pk in (key_properties + subkeys):
                pk_temp_subkey_select_list.append(sql.SQL('{}.{}').format(full_temp_table_name,
                                                                          sql.Identifier(pk)))
            insert_distinct_on = sql.SQL(', ').join(pk_temp_subkey_select_list)

            insert_distinct_order_by = sql.SQL(' ORDER BY {}, {}.{} DESC').format(
                insert_distinct_on,
                full_temp_table_name,
                sql.Identifier(SINGER_SEQUENCE))
        else:
            insert_distinct_on = pk_temp_select
            insert_distinct_order_by = distinct_order_by

        return sql.SQL('''
            WITH "pks" AS (
                SELECT DISTINCT ON ({pk_temp_select}) {pk_temp_select}
                FROM {temp_table}
                JOIN {table} ON {pk_where}{sequence_join}{distinct_order_by}
            )
            DELETE FROM {table} USING "pks" WHERE {cxt_where};
            INSERT INTO {table} (
                SELECT DISTINCT ON ({insert_distinct_on}) {temp_table}.*
                FROM {temp_table}
                LEFT JOIN {table} ON {pk_where}
                WHERE {pk_null}
                {insert_distinct_order_by}
            );
            DROP TABLE {temp_table};
            ''').format(table=full_table_name,
                        temp_table=full_temp_table_name,
                        pk_temp_select=pk_temp_select,
                        pk_where=pk_where,
                        cxt_where=cxt_where,
                        sequence_join=sequence_join,
                        distinct_order_by=distinct_order_by,
                        pk_null=pk_null,
                        insert_distinct_on=insert_distinct_on,
                        insert_distinct_order_by=insert_distinct_order_by)

    def persist_rows(self,
                     cur,
                     target_table_name,
                     temp_table_name,
                     target_table_json_schema,
                     key_properties,
                     records):
        target_schema = self.get_schema(cur, self.postgres_schema, target_table_name)

        headers = list(target_schema['schema']['properties'].keys())

        datetime_fields = [k for k,v in target_table_json_schema['properties'].items()
                           if v.get('format') == 'date-time']

        default_fields = {k: v.get('default') for k, v in target_table_json_schema['properties'].items()
                          if v.get('default') is not None}

        fields = set(headers +
                     [v['from'] for k, v in target_schema.get('mappings', {}).items()])

        records_iter = iter(records)

        def transform():
            try:
                record = next(records_iter)
                row = {}

                for field in fields:
                    value = record.get(field, None)

                    ## Serialize fields which are not present but have default values set
                    if field in default_fields \
                            and value is None:
                        value = default_fields[field]

                    ## Serialize datetime to postgres compatible format
                    if field in datetime_fields \
                            and value is not None:
                        value = self.get_postgres_datetime(value)

                    ## Serialize NULL default value
                    if value == RESERVED_NULL_DEFAULT:
                        self.logger.warning(
                            'Reserved {} value found in field: {}. Value will be turned into literal null'.format(
                                RESERVED_NULL_DEFAULT,
                                field))

                    if value is None:
                        value = RESERVED_NULL_DEFAULT

                    field_name = field

                    if field in target_table_json_schema['properties']:
                        field_name = self.get_mapping(target_schema,
                                                      field,
                                                      target_table_json_schema['properties'][field]) \
                                     or field

                    if not field_name in row \
                        or row[field_name] is None \
                        or row[field_name] == RESERVED_NULL_DEFAULT:

                        row[field_name] = value

                with io.StringIO() as out:
                    writer = csv.DictWriter(out, headers)
                    writer.writerow(row)
                    return out.getvalue()
            except StopIteration:
                return ''

        csv_rows = TransformStream(transform)

        copy = sql.SQL('COPY {}.{} ({}) FROM STDIN WITH (FORMAT CSV, NULL {})').format(
            sql.Identifier(self.postgres_schema),
            sql.Identifier(temp_table_name),
            sql.SQL(', ').join(map(sql.Identifier, headers)),
            sql.Literal(RESERVED_NULL_DEFAULT))
        cur.copy_expert(copy, csv_rows)

        pattern = re.compile(SINGER_LEVEL.format('[0-9]+'))
        subkeys = list(filter(lambda header: re.match(pattern, header) is not None, headers))

        update_sql = self.get_update_sql(target_table_name,
                                         temp_table_name,
                                         key_properties,
                                         subkeys)
        cur.execute(update_sql)

    def get_postgres_datetime(self, *args):
        if len(args) > 0:
            parsed_datetime = arrow.get(args[0])
        else:
            parsed_datetime = arrow.get() # defaults to UTC now
        return parsed_datetime.format('YYYY-MM-DD HH:mm:ss.SSSSZZ')

    def add_column(self, cur, table_schema, table_name, column_name, column_schema):
        data_type = json_schema.to_sql(column_schema)

        if not json_schema.is_nullable(column_schema) \
                and not self.is_table_empty(cur, table_schema, table_name):
            self.logger.warning('Forcing new column `{}.{}.{}` to be nullable due to table not empty.'.format(
                table_schema,
                table_name,
                column_name))
            data_type = json_schema.to_sql(json_schema.make_nullable(column_schema))

        to_execute = sql.SQL('ALTER TABLE {table_schema}.{table_name} ' +
                             'ADD COLUMN {column_name} {data_type};').format(
            table_schema=sql.Identifier(table_schema),
            table_name=sql.Identifier(table_name),
            column_name=sql.Identifier(column_name),
            data_type=sql.SQL(data_type))

        cur.execute(to_execute)

    def migrate_column(self, cur, table_schema, table_name, column_name, mapped_name):
        cur.execute(sql.SQL('UPDATE {table_schema}.{table_name} ' +
                            'SET {mapped_name} = {column_name};').format(
            table_schema=sql.Identifier(table_schema),
            table_name=sql.Identifier(table_name),
            mapped_name=sql.Identifier(mapped_name),
            column_name=sql.Identifier(column_name)))

    def drop_column(self, cur, table_schema, table_name, column_name):
        cur.execute(sql.SQL('ALTER TABLE {table_schema}.{table_name} ' +
                            'DROP COLUMN {column_name};').format(
            table_schema=sql.Identifier(table_schema),
            table_name=sql.Identifier(table_name),
            column_name=sql.Identifier(column_name)))

    def make_column_nullable(self, cur, table_schema, table_name, column_name):
        cur.execute(sql.SQL('ALTER TABLE {table_schema}.{table_name} ' +
                            'ALTER COLUMN {column_name} DROP NOT NULL;').format(
            table_schema=sql.Identifier(table_schema),
            table_name=sql.Identifier(table_name),
            column_name=sql.Identifier(column_name)))

    def set_table_metadata(self, cur, table_schema, table_name, metadata):
        """
        Given a Metadata dict, set it as the comment on the given table.
        :param self: Postgres
        :param cur: Pscyopg.Cursor
        :param table_schema: String
        :param table_name: String
        :param metadata: Metadata Dict
        :return: None
        """

        parsed_metadata = {}
        parsed_metadata['key_properties'] = metadata.get('key_properties', [])
        parsed_metadata['version'] = metadata.get('version')
        parsed_metadata['mappings'] = metadata.get('mappings', {})

        cur.execute(sql.SQL('COMMENT ON TABLE {}.{} IS {};').format(
            sql.Identifier(table_schema),
            sql.Identifier(table_name),
            sql.Literal(json.dumps(parsed_metadata))))


    def create_table(self, cur, table_schema, table_name, schema, key_properties, table_version):
        create_table_sql = sql.SQL('CREATE TABLE {}.{}').format(
                sql.Identifier(table_schema),
                sql.Identifier(table_name))

        cur.execute(sql.SQL('{} ();').format(create_table_sql))

        if key_properties:
            self.set_table_metadata(cur, table_schema, table_name,
                                    {'key_properties': key_properties,
                                     'version': table_version})

        for prop, column_json_schema in schema['properties'].items():
            self.add_column(cur, table_schema, table_name, prop, column_json_schema)

    def get_temp_table_name(self, stream_name):
        return stream_name + self.SEPARATOR + str(uuid.uuid4()).replace('-', '')

    def get_table_metadata(self, cur, table_schema, table_name):
        cur.execute(
            sql.SQL('SELECT obj_description(to_regclass({}));').format(
                sql.Literal('{}.{}'.format(table_schema, table_name))))
        comment = cur.fetchone()[0]

        if comment:
            try:
                comment_meta = json.loads(comment)
            except Exception as ex:
                message = 'Could not load table comment metadata'
                self.logger.exception(message)
                raise PostgresError(message, ex)
        else:
            comment_meta = None

        return comment_meta


    def add_column_mapping(self, cur, table_schema, table_name, column_name, mapped_name, mapped_schema):
        metadata = self.get_table_metadata(cur, table_schema, table_name)

        if not metadata:
            metadata = {}

        if not 'mappings' in metadata:
            metadata['mappings'] = {}

        metadata['mappings'][mapped_name] = {'type': json_schema.get_type(mapped_schema),
                                             'from': column_name}

        self.set_table_metadata(cur, table_schema, table_name, metadata)


    def is_table_empty(self, cur, table_schema, table_name):
        cur.execute(sql.SQL('SELECT COUNT(1) FROM {}.{};').format(
            sql.Identifier(table_schema),
            sql.Identifier(table_name)))

        return cur.fetchall()[0][0] == 0

    def get_schema(self, cur, table_schema, table_name):
        cur.execute(
            sql.SQL('SELECT column_name, data_type, is_nullable FROM information_schema.columns ') +
            sql.SQL('WHERE table_schema = {} and table_name = {};').format(
                sql.Literal(table_schema), sql.Literal(table_name)))

        properties = {}
        for column in cur.fetchall():
            properties[column[0]] = json_schema.from_sql(column[1], column[2] == 'YES')

        metadata = self.get_table_metadata(cur, table_schema, table_name)

        if metadata is None and not properties:
            return None
        elif metadata is None:
            metadata = {}

        metadata['name'] = table_name
        metadata['type'] = 'TABLE_SCHEMA'
        metadata['schema'] = {'properties': properties}

        return metadata

    def mapping_name(self, field, schema):
        return field + self.SEPARATOR + json_schema.sql_shorthand(schema)

    def get_mapping(self, metadata, field, schema):
        typed_field = self.mapping_name(field, schema)

        if 'mappings' in metadata \
                and typed_field in metadata['mappings']\
                and field == metadata['mappings'][typed_field]['from']:
            return typed_field

        return None

    def split_column(self, cur, table_schema, table_name, column_name, column_schema, existing_properties):
        ## column_name -> column_name__<current-type>, column_name__<new-type>
        existing_column_mapping = self.mapping_name(column_name, existing_properties[column_name])
        new_column_mapping = self.mapping_name(column_name, column_schema)

        ## Update existing properties
        existing_properties[existing_column_mapping] = json_schema.make_nullable(existing_properties[column_name])
        existing_properties[new_column_mapping] = json_schema.make_nullable(column_schema)

        ## Add new columns
        ### NOTE: all migrated columns will be nullable and remain that way

        #### Table Metadata
        self.add_column_mapping(cur, table_schema, table_name, column_name,
                                existing_column_mapping,
                                existing_properties[existing_column_mapping])
        self.add_column_mapping(cur, table_schema, table_name, column_name,
                                new_column_mapping,
                                existing_properties[new_column_mapping])

        #### Columns
        self.add_column(cur,
                        table_schema,
                        table_name,
                        existing_column_mapping,
                        existing_properties[existing_column_mapping])

        self.add_column(cur,
                        table_schema,
                        table_name,
                        new_column_mapping,
                        existing_properties[new_column_mapping])

        ## Migrate existing data
        self.migrate_column(cur,
                            table_schema,
                            table_name,
                            column_name,
                            existing_column_mapping)

        ## Drop existing column
        self.drop_column(cur,
                         table_schema,
                         table_name,
                         column_name)

        ## Remove column (field) from existing_properties
        del existing_properties[column_name]

    def merge_put_schemas(self, cur, table_schema, table_name, existing_schema, new_schema):
        new_properties = new_schema['properties']
        existing_properties = existing_schema['schema']['properties']
        for name, schema in new_properties.items():
            ## Mapping exists
            if self.get_mapping(existing_schema, name, schema) is not None:
                pass

            ## New column
            elif name not in existing_properties:

                existing_properties[name] = schema
                self.add_column(cur,
                                table_schema,
                                table_name,
                                name,
                                schema)

            ## Existing column non-nullable, new column is nullable
            elif not json_schema.is_nullable(existing_properties[name]) \
                    and json_schema.get_type(schema) \
                    == json_schema.get_type(json_schema.make_nullable(existing_properties[name])):

                existing_properties[name] = json_schema.make_nullable(existing_properties[name])
                self.make_column_nullable(cur,
                                          table_schema,
                                          table_name,
                                          name)

            ## Existing column, types compatible
            elif json_schema.to_sql(json_schema.make_nullable(schema)) \
                    == json_schema.to_sql(json_schema.make_nullable(existing_properties[name])):
                pass

            ## Column type change
            elif self.mapping_name(name, schema) not in existing_properties \
                and self.mapping_name(name, existing_properties[name]) not in existing_properties:

                self.split_column(cur,
                                  table_schema,
                                  table_name,
                                  name,
                                  schema,
                                  existing_properties)

            ## Error
            else:
                raise PostgresError(
                    'Cannot handle column type change for: {}.{} columns {} and {}. Name collision likely.'.format(
                        table_schema,
                        table_name,
                        name,
                        self.mapping_name(name, schema)
                    ))
