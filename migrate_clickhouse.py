#!/usr/bin/env python3
import argparse
import base64
import os
import re
import sys
from pathlib import Path


ENV_FILE = Path(__file__).resolve().with_name('.env')
SOURCE_ENV_PREFIX = 'CLICKHOUSE_SOURCE_'
TARGET_ENV_PREFIX = 'CLICKHOUSE_TARGET_'


def load_dotenv(path=ENV_FILE):
    values = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def connection_setting(cli_value, key, dotenv_values, default=None):
    if cli_value is not None:
        return cli_value
    if key in os.environ:
        return os.environ[key]
    if key in dotenv_values:
        return dotenv_values[key]
    return default


def source_setting(cli_value, key, dotenv_values, default=None):
    return connection_setting(cli_value, key, dotenv_values, default)


def redact(value, secrets):
    value = str(value)
    for secret in secrets:
        if secret:
            value = value.replace(secret, '***')
    return value


def connection_port(value, parser, option_name, environment_name):
    try:
        port = int(value)
    except (TypeError, ValueError):
        parser.error(f'{option_name} or {environment_name} must be an integer')
    if not 1 <= port <= 65535:
        parser.error(f'{option_name} or {environment_name} must be between 1 and 65535')
    return port


def source_port(value, parser):
    return connection_port(
        value, parser, '--source-port', 'CLICKHOUSE_SOURCE_PORT'
    )


ENVIRONMENT_SUFFIX_PATTERN = re.compile(r'^_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*$')


def environment_suffix(value, parser, option_name):
    if not ENVIRONMENT_SUFFIX_PATTERN.fullmatch(value):
        parser.error(
            f'{option_name} must be an environment suffix such as _qa or _v3_dev'
        )
    return value


def has_environment_suffix(name, suffix):
    return len(name) > len(suffix) and name.endswith(suffix)


def resolve_environment_selection(args, parser):
    known_suffixes = [
        environment_suffix(value, parser, '--known-non-production-suffix')
        for value in args.known_non_production_suffix
    ]
    if len(set(known_suffixes)) != len(known_suffixes):
        parser.error('--known-non-production-suffix values must not be duplicated')
    for index, suffix in enumerate(known_suffixes):
        if any(
            suffix.endswith(other) or other.endswith(suffix)
            for other in known_suffixes[index + 1:]
        ):
            parser.error('--known-non-production-suffix values must not overlap')

    if args.environment_suffix is None:
        return None, tuple(known_suffixes)
    if args.environment_suffix == 'prod':
        if not known_suffixes:
            parser.error(
                '--environment-suffix prod requires at least one '
                '--known-non-production-suffix'
            )
        return 'prod', tuple(known_suffixes)
    return (
        environment_suffix(args.environment_suffix, parser, '--environment-suffix'),
        tuple(known_suffixes),
    )


def environment_matches(name, selection, known_suffixes):
    if selection is None:
        return True
    if selection == 'prod':
        return not any(has_environment_suffix(name, suffix) for suffix in known_suffixes)
    return has_environment_suffix(name, selection)


def target_secure(value, parser):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    parser.error(
        '--target-secure or CLICKHOUSE_TARGET_SECURE must be true/false, yes/no, on/off, or 1/0'
    )
    return False



class TargetSecureAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, option_string == '--target-secure')


def add_target_secure_argument(parser):
    parser.add_argument(
        '--target-secure',
        dest='target_secure',
        action=TargetSecureAction,
        nargs=0,
        default=None,
        help='Use HTTPS for the target.',
    )
    parser.add_argument(
        '--no-target-secure',
        dest='target_secure',
        action=TargetSecureAction,
        nargs=0,
        default=None,
        help='Use plain HTTP for the target.',
    )


def resolve_target_settings(args, dotenv_values, parser):
    args.target_host = connection_setting(
        args.target_host, f'{TARGET_ENV_PREFIX}HOST', dotenv_values, 'localhost'
    )
    args.target_port = connection_port(
        connection_setting(
            args.target_port, f'{TARGET_ENV_PREFIX}PORT', dotenv_values, '8133'
        ),
        parser,
        '--target-port',
        'CLICKHOUSE_TARGET_PORT',
    )
    args.target_user = connection_setting(
        args.target_user, f'{TARGET_ENV_PREFIX}USER', dotenv_values, 'default'
    )
    args.target_password = connection_setting(
        args.target_password,
        f'{TARGET_ENV_PREFIX}PASSWORD',
        dotenv_values,
        '',
    )
    args.target_secure = target_secure(
        connection_setting(
            args.target_secure, f'{TARGET_ENV_PREFIX}SECURE', dotenv_values, False
        ),
        parser,
    )
    if not args.target_host:
        parser.error('--target-host or CLICKHOUSE_TARGET_HOST is required')
    return args


import clickhouse_connect

SKIP_DBS = {'system', 'information_schema', 'INFORMATION_SCHEMA', '__internal'}


def qi(value):
    return '`' + value.replace('`', '``') + '`'


def fq(database, name):
    return f'{qi(database)}.{qi(name)}'


def strip_cluster(sql):
    return re.sub(r'\s+ON\s+CLUSTER\s+`?[^`\s]+`?', '', sql, flags=re.I)


class DDLNormalizationError(ValueError):
    pass


def split_engine_arguments(text):
    arguments = []
    start = 0
    depth = 0
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == '\\':
                index += 2
                continue
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"', '`'}:
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            if depth == 0:
                raise DDLNormalizationError('unbalanced engine arguments')
            depth -= 1
        elif char == ',' and depth == 0:
            arguments.append(text[start:index].strip())
            start = index + 1
        index += 1
    if quote or depth:
        raise DDLNormalizationError('unbalanced engine arguments')
    arguments.append(text[start:].strip())
    return arguments


def engine_closing_parenthesis(sql, opening_index):
    depth = 0
    quote = None
    index = opening_index
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == '\\':
                index += 2
                continue
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"', '`'}:
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise DDLNormalizationError('unterminated engine argument list')


def is_string_literal(value):
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}


def localize_shared_mergetree(sql):
    match = re.search(
        r'\bENGINE\s*=\s*(Shared[A-Za-z0-9_]*MergeTree)\b\s*', sql, re.I
    )
    if not match:
        return sql, None

    source_engine = match.group(1)
    engine_key = source_engine.lower()
    if engine_key not in {'sharedmergetree', 'sharedreplacingmergetree'}:
        raise DDLNormalizationError(f'unsupported ClickHouse Cloud engine {source_engine}')
    if match.end() >= len(sql) or sql[match.end()] != '(':
        raise DDLNormalizationError(f'{source_engine} is missing its argument list')

    opening_index = match.end()
    closing_index = engine_closing_parenthesis(sql, opening_index)
    arguments = split_engine_arguments(sql[opening_index + 1:closing_index])
    if len(arguments) < 2 or not all(is_string_literal(value) for value in arguments[:2]):
        raise DDLNormalizationError(
            f'{source_engine} must begin with quoted path and replica arguments'
        )

    if engine_key == 'sharedmergetree':
        if len(arguments) != 2:
            raise DDLNormalizationError('SharedMergeTree must have only path and replica arguments')
        target_engine = 'MergeTree'
    else:
        target_engine = 'ReplacingMergeTree'
        if len(arguments) > 2:
            target_engine += '(' + ', '.join(arguments[2:]) + ')'

    normalized = sql[:match.start(1)] + target_engine + sql[closing_index + 1:]
    return normalized, (source_engine, target_engine)


def localize_shared_replacing_mergetree(sql):
    """Compatibility wrapper for the former normalizer function name."""
    normalized, metadata = localize_shared_mergetree(sql)
    return normalized, metadata[1] if metadata else None


def typ(engine, sql):
    engine = (engine or '').lower()
    if 'materializedview' in engine or 'materialized_view' in engine:
        return 'materialized_view'
    if engine == 'view' or re.search(r'\bCREATE\s+(OR\s+REPLACE\s+)?VIEW\b', sql, re.I):
        return 'view'
    if engine in {'liveview', 'windowview', 'dictionary'}:
        return engine
    return 'table'


def ddl_filename(database, name):
    def encode(value):
        return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')

    return f'{encode(database)}.{encode(name)}.sql'


def write_report(path, report):
    path.write_text('\n'.join(report) + ('\n' if report else ''))


DEFAULT_BATCH_SIZE = 10000


def copy_table_rows(source, target, name, query, batch_size):
    row_count = 0
    batch_count = 0
    pending = []
    column_names = None
    insert_context = None
    create_insert_context = getattr(target, 'create_insert_context', None)

    def flush(rows):
        nonlocal row_count, batch_count, insert_context
        if not rows:
            return
        if insert_context is None and create_insert_context is not None:
            insert_context = create_insert_context(
                table=name, column_names=column_names
            )
        if insert_context is not None:
            insert_context.data = rows
            target.insert(context=insert_context)
        else:
            target.insert(name, rows, column_names=column_names)
        row_count += len(rows)
        batch_count += 1

    with source.query_row_block_stream(
        query,
        settings={'max_block_size': batch_size},
    ) as stream:
        column_names = stream.source.column_names
        for block in stream:
            if not block:
                continue
            pending.extend(block)
            while len(pending) >= batch_size:
                flush(pending[:batch_size])
                del pending[:batch_size]
        flush(pending)
    return row_count, batch_count


def main():
    parser = argparse.ArgumentParser(
        description='Copy rows from ClickHouse base tables to a target server.'
    )
    parser.add_argument(
        '--source-host',
        help='ClickHouse Cloud hostname only (not a jdbc: URL, path, or query string).',
    )
    parser.add_argument('--source-port')
    parser.add_argument('--source-user')
    parser.add_argument('--source-password')
    parser.add_argument('--target-host')
    parser.add_argument('--target-port')
    parser.add_argument('--target-user')
    parser.add_argument('--target-password')
    add_target_secure_argument(parser)
    parser.add_argument(
        '--database',
        default='default',
        help='Source database to migrate (default: default).',
    )
    parser.add_argument(
        '--environment-suffix',
        help='Migrate names ending in this literal suffix (for example _qa), or prod for unsuffixed production names.',
    )
    parser.add_argument(
        '--known-non-production-suffix',
        action='append',
        default=[],
        help='Known non-production suffix for --environment-suffix prod; repeat for each suffix.',
    )
    parser.add_argument(
        '--all-databases',
        help='Migrate all non-system source databases instead of only --database.',
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    row_selection = parser.add_mutually_exclusive_group()
    row_selection.add_argument(
        '--rows',
        type=int,
        default=10,
        help='Maximum unordered sample rows to copy per successfully created base table (default: 10).',
    )
    row_selection.add_argument(
        '--all-rows',
        action='store_true',
        help='Copy all rows from each successfully created base table in insert batches.',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            'Maximum rows per target insert when copying table data '
            f'(default: {DEFAULT_BATCH_SIZE}). Small source blocks are coalesced.'
        ),
    )
    parser.add_argument('--output-dir', default='./clickhouse-migration-test')
    collision_policy = parser.add_mutually_exclusive_group()
    collision_policy.add_argument(
        '--drop-existing',
        action='store_true',
        help='Delete colliding target objects and their data before migration.',
    )
    collision_policy.add_argument(
        '--skip-existing',
        action='store_true',
        help='Preserve colliding target objects and migrate only missing objects.',
    )
    args = parser.parse_args()
    dotenv_values = load_dotenv()
    args.source_host = source_setting(
        args.source_host, f'{SOURCE_ENV_PREFIX}HOST', dotenv_values
    )
    args.source_port = source_port(
        source_setting(args.source_port, f'{SOURCE_ENV_PREFIX}PORT', dotenv_values, '8443'), parser
    )
    args.source_user = source_setting(
        args.source_user, f'{SOURCE_ENV_PREFIX}USER', dotenv_values, 'default'
    )
    args.source_password = source_setting(
        args.source_password, f'{SOURCE_ENV_PREFIX}PASSWORD', dotenv_values, ''
    )
    resolve_target_settings(args, dotenv_values, parser)
    if not args.source_host:
        parser.error('--source-host or CLICKHOUSE_SOURCE_HOST is required')
    if args.rows < 0:
        parser.error('--rows must be >= 0')
    if args.batch_size < 1:
        parser.error('--batch-size must be >= 1')
    if not args.all_databases and args.database in SKIP_DBS:
        parser.error('--database must not be a system database')
    environment_selection, known_non_production_suffixes = resolve_environment_selection(
        args, parser
    )

    output = Path(args.output_dir)
    (output / 'ddl').mkdir(parents=True, exist_ok=True)
    report_path = output / 'migration-report.txt'
    report = []
    failures = False

    def fail(message):
        nonlocal failures
        failures = True
        report.append(f'ERROR {message}')
        print(f'ERROR {message}', file=sys.stderr)

    try:
        source = clickhouse_connect.get_client(
            host=args.source_host,
            port=args.source_port,
            username=args.source_user,
            password=args.source_password,
            secure=True,
            connect_timeout=20,
            send_receive_timeout=300,
        )
        target = clickhouse_connect.get_client(
            host=args.target_host,
            port=args.target_port,
            username=args.target_user,
            password=args.target_password,
            secure=args.target_secure,
            connect_timeout=20,
            send_receive_timeout=300,
        )
    except Exception as exc:
        fail(f'CONNECT: {redact(exc, [args.source_password, args.target_password])}')
        write_report(report_path, report)
        return 1

    try:
        if args.all_databases:
            databases = [
                row[0]
                for row in source.query(
                    "SELECT name FROM system.databases "
                    "WHERE name NOT IN ('system','information_schema','INFORMATION_SCHEMA','__internal') "
                    'ORDER BY name'
                ).result_rows
            ]
        else:
            databases = [
                row[0]
                for row in source.query(
                    'SELECT name FROM system.databases WHERE name = %(database)s',
                    parameters={'database': args.database},
                ).result_rows
            ]
            if not databases:
                raise RuntimeError(f'Source database {args.database!r} does not exist')

        objects = []
        for database in databases:
            rows = source.query(
                'SELECT name, engine, create_table_query, is_temporary '
                'FROM system.tables WHERE database = %(database)s AND is_temporary = 0 '
                'ORDER BY name',
                parameters={'database': database},
            ).result_rows
            for name, engine, create_sql, _ in rows:
                if create_sql and environment_matches(
                    name, environment_selection, known_non_production_suffixes
                ):
                    objects.append(
                        {
                            'db': database,
                            'name': name,
                            'engine': engine,
                            'type': typ(engine, create_sql),
                            'ddl': create_sql,
                        }
                    )
        if environment_selection is not None:
            if not objects:
                raise RuntimeError(
                    f'No source objects match environment selection '
                    f'{environment_selection!r}'
                )
            databases = sorted({obj['db'] for obj in objects})
            report.append(
                f'ENVIRONMENT SELECTION {environment_selection}: '
                f'{len(objects)} objects'
            )
    except Exception as exc:
        fail(f'DISCOVERY: {exc}')
        write_report(report_path, report)
        return 1

    try:
        for obj in objects:
            ddl = strip_cluster(obj['ddl'])
            normalized_engine = None
            if obj['type'] == 'table':
                ddl, normalized_engine = localize_shared_mergetree(ddl)
            obj['target_ddl'] = ddl
            obj['normalized_engine'] = normalized_engine
    except DDLNormalizationError as exc:
        fail(f'DDL NORMALIZATION {fq(obj["db"], obj["name"])}: {exc}')
        write_report(report_path, report)
        return 1

    tables = [obj for obj in objects if obj['type'] == 'table']
    views = [obj for obj in objects if obj['type'] == 'view']
    materialized_views = [obj for obj in objects if obj['type'] == 'materialized_view']
    other_views = [obj for obj in objects if obj['type'] in {'liveview', 'windowview'}]
    dictionaries = [obj for obj in objects if obj['type'] == 'dictionary']
    target_objects = tables + views + materialized_views + other_views

    schema_path = output / 'schema.sql'
    try:
        with schema_path.open('w') as schema:
            for database in databases:
                schema.write(f'CREATE DATABASE IF NOT EXISTS {qi(database)};\n')
            schema.write('\n')
            for obj in objects:
                ddl = obj['target_ddl'].rstrip(';') + ';\n'
                schema.write(f'-- {obj["type"]} {fq(obj["db"], obj["name"])}\n{ddl}\n')
                (output / 'ddl' / ddl_filename(obj['db'], obj['name'])).write_text(ddl)
    except Exception as exc:
        fail(f'WRITE SCHEMA: {exc}')
        write_report(report_path, report)
        return 1

    try:
        existing_objects = {
            (database, name)
            for database, name in target.query(
                'SELECT database, name FROM system.tables'
            ).result_rows
        }
    except Exception as exc:
        fail(f'TARGET PREFLIGHT: {exc}')
        write_report(report_path, report)
        return 1

    collision_keys = {
        (obj['db'], obj['name'])
        for obj in target_objects
        if (obj['db'], obj['name']) in existing_objects
    }
    collisions = [fq(database, name) for database, name in sorted(collision_keys)]
    if collisions and not args.drop_existing and not args.skip_existing:
        fail(
            'TARGET COLLISIONS: '
            + ', '.join(collisions)
            + ' (use a fresh target, --drop-existing, or --skip-existing)'
        )
        write_report(report_path, report)
        return 1

    preserved_objects = len(collision_keys) if args.skip_existing else 0

    if args.drop_existing:
        for obj in views + materialized_views + other_views:
            name = fq(obj['db'], obj['name'])
            try:
                target.command(f'DROP VIEW IF EXISTS {name}')
                target.command(f'DROP TABLE IF EXISTS {name}')
                report.append(f'DROP {obj["type"].upper()} {name}')
            except Exception as exc:
                fail(f'DROP {obj["type"].upper()} {name}: {exc}')
        for obj in tables:
            name = fq(obj['db'], obj['name'])
            try:
                target.command(f'DROP TABLE IF EXISTS {name}')
                report.append(f'DROP TABLE {name}')
            except Exception as exc:
                fail(f'DROP TABLE {name}: {exc}')

    available_databases = set()
    for database in databases:
        try:
            target.command(f'CREATE DATABASE IF NOT EXISTS {qi(database)}')
            available_databases.add(database)
            report.append(f'DB {database}')
        except Exception as exc:
            fail(f'DB {database}: {exc}')

    created_tables = set()
    for obj in tables:
        name = fq(obj['db'], obj['name'])
        if args.skip_existing and (obj['db'], obj['name']) in collision_keys:
            report.append(f'SKIP TABLE {name}: target object already exists (preserved)')
            continue
        if obj['db'] not in available_databases:
            report.append(f'SKIP TABLE {name}: database creation failed')
            continue
        try:
            target.command(obj['target_ddl'])
            created_tables.add((obj['db'], obj['name']))
            if obj['normalized_engine']:
                source_engine, target_engine = obj['normalized_engine']
                report.append(f'NORMALIZE TABLE {name}: {source_engine} -> {target_engine}')
            report.append(f'TABLE {name}')
        except Exception as exc:
            fail(f'TABLE {name}: {exc}')

    total_rows = 0
    for obj in tables:
        name = fq(obj['db'], obj['name'])
        if args.skip_existing and (obj['db'], obj['name']) in collision_keys:
            report.append(f'SKIP DATA {name}: target object already exists (preserved)')
            continue
        if (obj['db'], obj['name']) not in created_tables:
            report.append(f'SKIP DATA {name}: table DDL did not succeed')
            continue
        try:
            data_query = f'SELECT * FROM {name}'
            if not args.all_rows:
                data_query += f' LIMIT {args.rows}'
            row_count, batch_count = copy_table_rows(
                source, target, name, data_query, args.batch_size
            )
            total_rows += row_count
            report.append(f'DATA {name}: {row_count} rows ({batch_count} batches)')
        except Exception as exc:
            fail(f'DATA {name}: {exc}')

    for obj in views + materialized_views + other_views:
        name = fq(obj['db'], obj['name'])
        if args.skip_existing and (obj['db'], obj['name']) in collision_keys:
            report.append(f'SKIP {obj["type"].upper()} {name}: target object already exists (preserved)')
            continue
        if obj['db'] not in available_databases:
            report.append(f'SKIP {obj["type"].upper()} {name}: database creation failed')
            continue
        try:
            target.command(obj['target_ddl'])
            report.append(f'{obj["type"].upper()} {name}')
        except Exception as exc:
            fail(f'{obj["type"].upper()} {name}: {exc}')

    for obj in dictionaries:
        report.append(f'SKIP DICTIONARY {fq(obj["db"], obj["name"])}')

    write_report(report_path, report)
    print(
        f'Databases: {len(databases)} | Tables: {len(tables)} | '
        f'Views: {len(views)} | MVs: {len(materialized_views)} | '
        f'Existing objects preserved: {preserved_objects} | '
        f'Dictionaries skipped: {len(dictionaries)} | Rows inserted: {total_rows}'
    )
    print(f'Schema: {schema_path}\nReport: {report_path}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
