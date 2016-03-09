import os
import sys
import socket
from urlparse import urlparse
from itertools import chain
import threading
from select import select
from cStringIO import StringIO as BytesIO


SYM_CRLF = '\r\n'

DEFAULT_PRIORITY = 2 ** 31

DEFAULT_TTR = 120

SERVER_CLOSED_CONNECTION_ERROR = "Connection closed by server."


class BeanstalkError(Exception):
    pass


class ConnectionError(BeanstalkError):
    pass


class TimeoutError(BeanstalkError):
    pass


class ResponseError(BeanstalkError):
    pass


class InvalidResponse(BeanstalkError):
    pass


class ConnectionPool(object):
    @classmethod
    def from_url(cls, url, **kwargs):
        url = urlparse(url)
        url_options = {}
        url_options.update({
            'host': url.hostname,
            'port': int(url.port or 11300)})
        kwargs.update(url_options)
        return cls(**kwargs)

    def __init__(self, max_connections=None, **kwargs):
        max_connections = max_connections or 2 ** 31
        if not isinstance(max_connections, int) or max_connections < 0:
            raise ValueError('"max_connections" must be a positive integer')

        self.connection_options = kwargs
        self.max_connections = max_connections
        self.reset()

    def reset(self):
        self.pid = os.getpid()
        self._created_connections = 0
        self._available_connections = []
        self._in_use_connections = set()
        self._check_lock = threading.Lock()

    def _checkpid(self):
        if self.pid != os.getpid():
            with self._check_lock:
                if self.pid == os.getpid():
                    return
                self.disconnect()
                self.reset()

    def get_connection(self):
        self._checkpid()
        try:
            connection = self._available_connections.pop()
        except IndexError:
            connection = self.make_connection()
        self._in_use_connections.add(connection)
        return connection

    def make_connection(self):
        if self._created_connections >= self.max_connections:
            raise ConnectionError("Too many connections")
        self._created_connections += 1
        return Connection(**self.connection_options)

    def release(self, connection):
        self._checkpid()
        if connection.pid != self.pid:
            return
        self._in_use_connections.remove(connection)
        self._available_connections.append(connection)

    def disconnect(self):
        all_conns = chain(self._available_connections,
                          self._in_use_connections)
        for conn in all_conns:
            conn.disconnect()


class Reader(object):
    def __init__(self, socket, socket_read_size):
        self._sock = socket
        self.socket_read_size = socket_read_size
        self._buffer = BytesIO()
        self.bytes_written = 0
        self.bytes_read = 0

    @property
    def length(self):
        return self.bytes_written - self.bytes_read

    def _read_from_socket(self, length=None):
        socket_read_size = self.socket_read_size
        buf = self._buffer
        buf.seek(self.bytes_written)
        marker = 0

        try:
            while True:
                data = self._sock.recv(socket_read_size)
                if isinstance(data, bytes) and len(data) == 0:
                    raise socket.error(SERVER_CLOSED_CONNECTION_ERROR)
                buf.write(data)
                data_length = len(data)
                self.bytes_written += data_length
                marker += data_length

                if length is not None and length > marker:
                    continue
                break
        except socket.timeout:
            raise TimeoutError("Timeout reading from socket")
        except socket.error:
            e = sys.exc_info()[1]
            raise ConnectionError("Error while reading from socket: %s" %
                                  (e.args,))

    def read(self, length):
        length = length + 2
        if length > self.length:
            self._read_from_socket(length - self.length)

        self._buffer.seek(self.bytes_read)
        data = self._buffer.read(length)
        self.bytes_read += len(data)

        if self.bytes_read == self.bytes_written:
            self.purge()

        return data[:-2]

    def readline(self):
        buf = self._buffer
        buf.seek(self.bytes_read)
        data = buf.readline()
        while not data.endswith(SYM_CRLF):
            self._read_from_socket()
            buf.seek(self.bytes_read)
            data = buf.readline()

        self.bytes_read += len(data)

        if self.bytes_read == self.bytes_written:
            self.purge()

        return data[:-2]

    def purge(self):
        self._buffer.seek(0)
        self._buffer.truncate()
        self.bytes_written = 0
        self.bytes_read = 0

    def close(self):
        try:
            self.purge()
            self._buffer.close()
        except:
            pass

        self._buffer = None
        self._sock = None


class BaseParser(object):
    def __init__(self, socket_read_size):
        self.socket_read_size = socket_read_size
        self._sock = None
        self._buffer = None

    def on_connect(self, connection):
        self._sock = connection._sock
        self._buffer = Reader(self._sock, self.socket_read_size)
        self.encoding = connection.encoding

    def on_disconnect(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        self.encoding = None

    def can_read(self):
        return self._buffer and bool(self._buffer_length)

    def read_response(self):
        response = self._buffer.readline()
        if not response:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)
        response = response.split()
        return response[0], response[1:]

    def read(self, size):
        response = self._buffer.read(size)
        if not response:
            raise ConnectionError(SERVER_CLOSED_CONNECTION_ERROR)
        return response


class Connection(object):
    def __init__(self, host='localhost', port=11300, socket_timeout=None,
                 retry_on_timeout=False, socket_connect_timeout=None,
                 socket_keepalive=False, socket_keepalive_options=None,
                 encoding='utf-8', socket_read_size=65536):
        self.pid = os.getpid()
        self.host = host
        self.port = port
        self.encoding = encoding
        self.socket_timeout = socket_timeout
        self.socket_connect_timeout = socket_connect_timeout
        self.socket_keepalive = socket_keepalive
        self.socket_keepalive_options = socket_keepalive_options
        self.retry_on_timeout = retry_on_timeout
        self._sock = None
        self._parser = BaseParser(socket_read_size=socket_read_size)

    def connect(self):
        if self._sock:
            return

        try:
            sock = self._connect()
        except socket.timeout:
            raise TimeoutError("Timeout connecting to server")
        except socket.error:
            e = sys.exc_info()[1]
            raise ConnectionError(self._error_message(e))

        self._sock = sock
        try:
            self.on_connect()
        except BeanstalkError:
            self.disconnect()
            raise

    def _connect(self):
        err = None
        for res in socket.getaddrinfo(self.host, self.port, 0,
                                      socket.SOCK_STREAM):
            family, socktype, proto, canonname, socket_address = res
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                if self.socket_keepalive:
                    sock.setsockopt(
                        socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    for k, v in self.socket_keepalive_options.iteritems():
                        sock.setsockopt(socket.SOL_TCP, k, v)

                sock.settimeout(self.socket_connect_timeout)

                sock.connect(socket_address)

                sock.settimeout(self.socket_timeout)
                return sock

            except socket.error as _:
                err = _
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        raise socket.error("socket.getaddrinfo returned empty list")

    def _error_message(self, exception):
        if len(exception.args) == 1:
            return "Error connecting to %s:%s. %s." % \
                (self.host, self.port, exception.args[0])
        else:
            return "Error %s connecting to %s:%s. %s." % \
                (exception.args[0], self.host, self.port, exception.args[1])

    def on_connect(self):
        self._parser.on_connect(self)

    def disconnect(self):
        self._parser.on_disconnect()
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
            self._sock.close()
        except socket.error:
            pass
        self._sock = None

    def can_read(self, timeout=0):
        sock = self._sock
        if not sock:
            self.connect()
            sock = self._sock
        return self._parser.can_read() or \
            bool(select([sock], [], [], timeout)[0])

    def pack_command(self, command, body, *args):
        output = []
        # TODO handle encoding
        output.append(command)
        for arg in args:
            output.append(' ' + str(arg))
        if body is not None:
            output.append(' ' + str(len(body)))
            output.append(SYM_CRLF)
            output.append(body)
        output.append(SYM_CRLF)
        return ''.join(output)

    def send_command(self, command, *args, **kwargs):
        command = self.pack_command(command, kwargs.get('body'), *args)
        if not self._sock:
            self.connect()
        try:
            if isinstance(command, str):
                command = [command]
            for item in command:
                self._sock.sendall(item)
        except socket.timeout:
            self.disconnect()
            raise TimeoutError("Timeout writing to socket")
        except socket.error:
            e = sys.exc_info()[1]
            self.disconnect()
            if len(e.args) == 1:
                errno, errmsg = 'UNKNOWN', e.args[0]
            else:
                errno = e.args[0]
                errmsg = e.args[1]
            raise ConnectionError("Error %s while writing to socket. %s." %
                                  (errno, errmsg))
        except:
            self.disconnect()
            raise

    def read_response(self):
        try:
            response = self._parser.read_response()
        except:
            self.disconnect()
            raise
        if isinstance(response, ResponseError):
            raise response
        return response

    def read_body(self, size):
        try:
            response = self._parser.read(size)
        except:
            self.disconnect()
            raise
        if isinstance(response, ResponseError):
            raise response
        return response


def dict_merge(*dicts):
    merged = {}
    for d in dicts:
        merged.update(d)
    return merged


def parse_body(connection, response, **kwargs):
    (status, results) = response
    job_id = results[0]
    job_size = results[1]
    body = connection.read_body(int(job_size))
    if job_size > 0 and not body:
        raise ResponseError()
    return job_id, body


class Beanstalk(object):
    RESPONSE_CALLBACKS = dict_merge(
        {'reserve': parse_body}
    )
    EXPECTED_OK = dict_merge(
        {'reserve': ['RESERVED'],
         'delete': ['DELETED'],
         'put': ['INSERTED']}

    )
    EXPECTED_ERR = dict_merge(
        {'reserve': ['DEADLINE_SOON', 'TIMED_OUT'],
         'delete': ['DELETED', 'NOT_FOUND'],
         'put': ['JOB_TOO_BIG', 'BURIED', 'DRAINING']}
    )

    @classmethod
    def from_url(cls, url, **kwargs):
        connection_pool = ConnectionPool.from_url(url, **kwargs)
        return cls(connection_pool=connection_pool)

    def __init__(self, host='localhost', port=11300, socket_timeout=None,
                 socket_connect_timeout=None, socket_keepalive=None,
                 retry_on_timeout=False, socket_keepalive_options=None,
                 max_connections=None, connection_pool=None):
        if not connection_pool:
            self.host = host
            self.port = port
            self.socket_timeout = socket_timeout
            kwargs = {
                'host': host,
                'port': port,
                'socket_connect_timeout': socket_connect_timeout,
                'socket_keepalive': socket_keepalive,
                'socket_keepalive_options': socket_keepalive_options,
                'socket_timeout': socket_timeout,
                'retry_on_timeout': retry_on_timeout,
                'max_connections': max_connections
            }

            connection_pool = ConnectionPool(**kwargs)
        self.connection_pool = connection_pool
        self.response_callbacks = self.__class__.RESPONSE_CALLBACKS.copy()
        self.expected_ok = self.__class__.EXPECTED_OK.copy()
        self.expected_err = self.__class__.EXPECTED_ERR.copy()

    def execute_command(self, *args, **kwargs):
        pool = self.connection_pool
        command_name = args[0]
        connection = pool.get_connection()
        try:
            connection.send_command(*args, **kwargs)
            return self.parse_response(connection, command_name, **kwargs)
        except (ConnectionError, TimeoutError) as e:
            connection.disconnect()
            if not connection.retry_on_timeout and isinstance(e, TimeoutError):
                raise
            connection.send_command(*args, **kwargs)
            return self.parse_response(connection, command_name, **kwargs)
        finally:
            pool.release(connection)

    def parse_response(self, connection, command_name, **kwargs):
        response = connection.read_response()
        status, results = response
        if status in self.expected_ok[command_name]:
            if command_name in self.response_callbacks:
                return self.response_callbacks[command_name](
                        connection, response, **kwargs)
            return response
        elif status in self.expected_err[command_name]:
            raise ResponseError(command_name, status, results)
        else:
            raise InvalidResponse(command_name, status, results)

        return response

    def put(self, body, priority=DEFAULT_PRIORITY, delay=0, ttr=DEFAULT_TTR):
        assert isinstance(body, str), 'body must be str'
        job_id = self.execute_command('put', priority, delay, ttr, body=body)
        return job_id

    def reserve(self, timeout=None):
        if timeout is not None:
            return self.execute_command('reserve-with-timeout', timeout)
        else:
            return self.execute_command('reserve')

    def delete(self, job_id):
        self.execute_command('delete', job_id)