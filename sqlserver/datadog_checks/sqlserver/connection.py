# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

from contextlib import contextmanager

from six import raise_from

from datadog_checks.base import AgentCheck

try:
    import adodbapi
except ImportError:
    adodbapi = None

try:
    import pyodbc
except ImportError:
    pyodbc = None

DATABASE_EXISTS_QUERY = 'select name from sys.databases;'


class SQLConnectionError(Exception):
    """Exception raised for SQL instance connection issues"""

    pass


class Connection(object):
    """Manages the connection to a SQL Server instance."""

    DEFAULT_COMMAND_TIMEOUT = 5
    DEFAULT_DATABASE = 'master'
    DEFAULT_DRIVER = 'SQL Server'
    DEFAULT_DB_KEY = 'database'
    PROC_GUARD_DB_KEY = 'proc_only_if_database'

    valid_adoproviders = ['SQLOLEDB', 'MSOLEDBSQL', 'SQLNCLI11']
    default_adoprovider = 'SQLOLEDB'

    def __init__(self, init_config, instance_config, service_check_handler, logger):
        self.instance = instance_config
        self.service_check_handler = service_check_handler
        self.log = logger

        # mapping of raw connections based on conn_key to different databases
        self._conns = {}
        self.timeout = int(self.instance.get('command_timeout', self.DEFAULT_COMMAND_TIMEOUT))
        self.existing_databases = None

        self.adoprovider = self.default_adoprovider

        self.valid_connectors = []
        if adodbapi is not None:
            self.valid_connectors.append('adodbapi')
        if pyodbc is not None:
            self.valid_connectors.append('odbc')

        self.connector = init_config.get('connector', 'adodbapi')
        if self.connector.lower() not in self.valid_connectors:
            self.log.error("Invalid database connector %s, defaulting to adodbapi", self.connector)
            self.connector = 'adodbapi'

        self.adoprovider = init_config.get('adoprovider', self.default_adoprovider)
        if self.adoprovider.upper() not in self.valid_adoproviders:
            self.log.error(
                "Invalid ADODB provider string %s, defaulting to %s", self.adoprovider, self.default_adoprovider
            )
            self.adoprovider = self.default_adoprovider

        self.log.debug('Connection initialized.')

    @contextmanager
    def get_managed_cursor(self):
        cursor = self.get_cursor(self.DEFAULT_DB_KEY)
        yield cursor
        self.close_cursor(cursor)

    def get_cursor(self, db_key, db_name=None):
        """
        Return a cursor to execute query against the db
        Cursor are cached in the self.connections dict
        """
        conn_key = self._conn_key(db_key, db_name)
        try:
            conn = self._conns[conn_key]
        except KeyError:
            # We catch KeyError to avoid leaking the auth info used to compose the key
            # FIXME: we should find a better way to compute unique keys to map opened connections other than
            # using auth info in clear text!
            raise SQLConnectionError("Cannot find an opened connection for host: {}".format(self.instance.get('host')))
        return conn.cursor()

    def close_cursor(self, cursor):
        """
        We close the cursor explicitly b/c we had proven memory leaks
        We handle any exception from closing, although according to the doc:
        "in adodbapi, it is NOT an error to re-close a closed cursor"
        """
        try:
            cursor.close()
        except Exception as e:
            self.log.warning("Could not close adodbapi cursor\n%s", e)

    def check_database(self):
        with self.open_managed_default_database():
            db_exists, context = self._check_db_exists()

        return db_exists, context

    @contextmanager
    def open_managed_default_database(self):
        with self._open_managed_db_connections(None, db_name=self.DEFAULT_DATABASE):
            yield

    @contextmanager
    def open_managed_default_connection(self):
        with self._open_managed_db_connections(self.DEFAULT_DB_KEY):
            yield

    @contextmanager
    def _open_managed_db_connections(self, db_key, db_name=None):
        self.open_db_connections(db_key, db_name)
        yield
        self.close_db_connections(db_key, db_name)

    def open_db_connections(self, db_key, db_name=None):
        """
        We open the db connections explicitly, so we can ensure they are open
        before we use them, and are closable, once we are finished. Open db
        connections keep locks on the db, presenting issues such as the SQL
        Server Agent being unable to stop.
        """

        conn_key = self._conn_key(db_key, db_name)

        dsn, host, username, password, database, driver = self._get_access_info(db_key, db_name)

        cs = self.instance.get('connection_string', '')
        cs += ';' if cs != '' else ''

        try:
            if self.get_connector() == 'adodbapi':
                cs += self._conn_string_adodbapi(db_key, db_name=db_name)
                # autocommit: true disables implicit transaction
                rawconn = adodbapi.connect(cs, {'timeout': self.timeout, 'autocommit': True})
            else:
                cs += self._conn_string_odbc(db_key, db_name=db_name)
                rawconn = pyodbc.connect(cs, timeout=self.timeout)

            self.service_check_handler(AgentCheck.OK, host, database)

            if conn_key not in self._conns:
                self._conns[conn_key] = rawconn
            else:
                try:
                    # explicitly trying to avoid leaks...
                    self._conns[conn_key].close()
                except Exception as e:
                    self.log.info("Could not close adodbapi db connection\n%s", e)

                self._conns[conn_key] = rawconn
        except Exception as e:
            cx = "{} - {}".format(host, database)
            message = "Unable to connect to SQL Server for instance {}: {}".format(cx, repr(e))

            password = self.instance.get('password')
            if password is not None:
                message = message.replace(password, "*" * 6)

            self.service_check_handler(AgentCheck.CRITICAL, host, database, message)

            raise_from(SQLConnectionError(message), None)

    def close_db_connections(self, db_key, db_name=None):
        """
        We close the db connections explicitly b/c when we don't they keep
        locks on the db. This presents as issues such as the SQL Server Agent
        being unable to stop.
        """
        conn_key = self._conn_key(db_key, db_name)
        if conn_key not in self._conns:
            return

        try:
            self._conns[conn_key].close()
            del self._conns[conn_key]
        except Exception as e:
            self.log.warning("Could not close adodbapi db connection\n%s", e)

    def _check_db_exists(self):
        """
        Check if the database we're targeting actually exists
        If not then we won't do any checks
        This allows the same config to be installed on many servers but fail gracefully
        """

        dsn, host, username, password, database, driver = self._get_access_info(self.DEFAULT_DB_KEY)
        context = "{} - {}".format(host, database)
        if self.existing_databases is None:
            cursor = self.get_cursor(None, self.DEFAULT_DATABASE)

            try:
                self.existing_databases = {}
                cursor.execute(DATABASE_EXISTS_QUERY)
                for row in cursor:
                    self.existing_databases[row.name] = True

            except Exception as e:
                self.log.error("Failed to check if database %s exists: %s", database, e)
                return False, context
            finally:
                self.close_cursor(cursor)

        return database in self.existing_databases, context

    def get_connector(self):
        connector = self.instance.get('connector', self.connector)
        if connector != self.connector:
            if connector.lower() not in self.valid_connectors:
                self.log.warning("Invalid database connector %s using default %s", connector, self.connector)
                connector = self.connector
            else:
                self.log.debug("Overriding default connector for %s with %s", self.instance['host'], connector)
        return connector

    def _get_adoprovider(self):
        provider = self.instance.get('adoprovider', self.default_adoprovider)
        if provider != self.adoprovider:
            if provider.upper() not in self.valid_adoproviders:
                self.log.warning("Invalid ADO provider %s using default %s", provider, self.adoprovider)
                provider = self.adoprovider
            else:
                self.log.debug("Overriding default ADO provider for %s with %s", self.instance['host'], provider)
        return provider

    def _get_access_info(self, db_key, db_name=None):
        """Convenience method to extract info from instance"""
        dsn = self.instance.get('dsn')
        host = self.instance.get('host')
        username = self.instance.get('username')
        password = self.instance.get('password')
        database = self.instance.get(db_key) if db_name is None else db_name
        driver = self.instance.get('driver')
        if not dsn:
            if not host:
                host = '127.0.0.1,1433'
            if not database:
                database = self.DEFAULT_DATABASE
            if not driver:
                driver = self.DEFAULT_DRIVER
        return dsn, host, username, password, database, driver

    def _conn_key(self, db_key, db_name=None):
        """Return a key to use for the connection cache"""
        dsn, host, username, password, database, driver = self._get_access_info(db_key, db_name)
        return '{}:{}:{}:{}:{}:{}'.format(dsn, host, username, password, database, driver)

    def _conn_string_odbc(self, db_key, conn_key=None, db_name=None):
        """Return a connection string to use with odbc"""
        if conn_key:
            dsn, host, username, password, database, driver = conn_key.split(":")
        else:
            dsn, host, username, password, database, driver = self._get_access_info(db_key, db_name)

        conn_str = ''
        if dsn:
            conn_str = 'DSN={};'.format(dsn)

        if driver:
            conn_str += 'DRIVER={};'.format(driver)
        if host:
            conn_str += 'Server={};'.format(host)
        if database:
            conn_str += 'Database={};'.format(database)

        if username:
            conn_str += 'UID={};'.format(username)
        self.log.debug("Connection string (before password) %s", conn_str)
        if password:
            conn_str += 'PWD={};'.format(password)
        return conn_str

    def _conn_string_adodbapi(self, db_key, conn_key=None, db_name=None):
        """Return a connection string to use with adodbapi"""
        if conn_key:
            _, host, username, password, database, _ = conn_key.split(":")
        else:
            _, host, username, password, database, _ = self._get_access_info(db_key, db_name)

        provider = self._get_adoprovider()
        conn_str = 'Provider={};Data Source={};Initial Catalog={};'.format(provider, host, database)

        if username:
            conn_str += 'User ID={};'.format(username)
        self.log.debug("Connection string (before password) %s", conn_str)
        if password:
            conn_str += 'Password={};'.format(password)
        if not username and not password:
            conn_str += 'Integrated Security=SSPI;'
        return conn_str
