#!/usr/bin/env python3
##
## Modified FakeWake - Multiple Device Reset Control
## This software is supplied "as is" with no warranties either expressed or implied.
##
## For non-commercial use only.

# imports
import argparse
import configparser
import gpiozero
import grp
import logging
import os
import pwd
import select
import socket
import io
import subprocess
import sys
import time
import threading

from gpiozero import OutputDevice

class JetsonDevice:
    def __init__(self, name, reset_gpio):
        self.name = name
        self.reset_gpio = reset_gpio
        # Fix typo: intital_value -> initial_value
        self.reset_control = OutputDevice(reset_gpio, active_high=True, initial_value=False)
    
    def reset(self):
        try:
            print(f"Resetting {self.name}...")
            self.reset_control.on()
            time.sleep(0.5)
            self.reset_control.off()
            print(f"{self.name} reset completed")
            return True
        except Exception as e:
            print(f"Error resetting {self.name}: {e}")
            return False
        
    def get_status(self):
        """Return basic device status"""
        return {
            'name': self.name,
            'reset_gpio': self.reset_gpio,
            'reset_active': self.reset_control.is_active,
            'status': 'ready'
        }
    
    def cleanup(self):
        """Clean up GPIO resources"""
        if hasattr(self, 'reset_control'):
            self.reset_control.close()

# Global variables
DEVICES = {}  # Dictionary to store device objects
stop_threads = False
last_action_time = {}  # Per-device timing

def valid_host(ipaddr):
    """Validate IP address against HOST_ALLOW and HOSTS_DENY"""
    logging.debug('HOSTS_ALLOW %s' % HOSTS_ALLOW)
    logging.debug('HOSTS_DENY %s' % HOSTS_DENY)
    
    ipaddr = str(ipaddr)
    logging.debug('ipaddr to check %s' % ipaddr)

    if ipaddr in HOSTS_ALLOW:
        return True

    if '*' in HOSTS_DENY:
        return False

    if ipaddr in HOSTS_DENY:
        return False

    return True

def _daemonize_me():
    """Daemonize the process"""
    umask = 0
    workingdir = '/'
    maxfd = 1024
    if (hasattr(os, 'devnull')):
        devnull = os.devnull
    else:
        devnull = '/dev/null'

    try:
        pid = os.fork()
    except OSError as e:
        raise Exception('%s [%d]' % (e.strerror, e.errno))

    if pid == 0:
        os.setsid()
        try:
            pid = os.fork()
        except OSError as e:
            raise Exception('%s [%d]' % (e.strerror, e.errno))
        if pid == 0:
            os.chdir(workingdir)
            os.umask(umask)
        else:
            os._exit(0)
    else:
        os._exit(0)
    
    # Close all open file descriptors
    try:
        mfd = os.sysconf('SC_OPEN_MAX')
    except (AttributeError, ValueError):
        mfd = maxfd
    for fd in range(0, mfd):
        try:
            os.close(fd)
        except OSError:
            pass

    # Redirect stdio
    os.open(devnull, os.O_RDWR)  # stdin
    os.dup2(0, 1)  # stdout
    os.dup2(0, 2)  # stderr

def reset_device(device_name, duration):
    """Reset a specific device"""
    global last_action_time
    
    if device_name not in DEVICES:
        logging.error(f"Device {device_name} not found")
        return False

    device = DEVICES[device_name]
    current_time = time.time()
    
    # Check timing interval per device
    if device_name not in last_action_time:
        last_action_time[device_name] = 0
    
    if current_time > last_action_time[device_name] + MIN_INTERVAL:
        logging.info(f'Resetting device {device_name} for {duration} seconds at {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_time))}')
        last_action_time[device_name] = current_time
        
        device.reset_control.on()
        time.sleep(duration)
        device.reset_control.off()
        return True
    else:
        logging.debug(f'Too soon after last action for device {device_name}')
        return False

def pinger(targets, interval):
    """Thread to ping target systems and set result accordingly"""
    global PINGABLE
    
    logging.debug('Ping targets: %s' % targets)
    
    pingthings = {}
    for t in targets:
        if t.strip():  # Only add non-empty targets
            pingthings[t] = gpiozero.PingServer(t)
    
    logging.debug(pingthings)
    
    while not stop_threads:
        result = '<br>'
        for p in pingthings:
            result += '&nbsp;&nbsp;&nbsp;&nbsp;%s: %s<br>' % (p, ('No', 'Yes')[pingthings[p].value])
        PINGABLE = result
        time.sleep(interval)
    
    # Cleanup
    for p in pingthings:
        pingthings[p].close()
    logging.debug('Pinger exiting')

def webserver(host, port):
    """Simple web server for device control"""
    global last_action_time

    base_header = 'HTTP/1.0 '
    ok_header = '200 OK\n\n'
    html_header = '<!DOCTYPE HTML>\n<html><head><title>FakeWake - Multi Device Control</title>\n'
    clacks_header = '<meta http-equiv="X-Clacks-Overhead" content="GNU Terry Pratchett" />\n'
    refresh_header = '<meta http-equiv="refresh" content="%s;/">' % WEBSERVER_RELOAD_DELAY
    end_header = '</head><body>'
    
    error403 = base_header + ok_header + html_header + clacks_header + end_header
    error403 += '<h2>403 Forbidden</h2></body></html>'
    error404 = base_header + ok_header + html_header + clacks_header + end_header
    error404 += '<h2>404: This space unintentionally left blank</h2></body></html>'
    error405 = base_header + '405 Method Not Allowed\n' + html_header + clacks_header + end_header
    error405 += '<h2>405 Method Not Allowed</h2></body></html>'
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    logging.debug('binding to %s:%s' % (host, port))
    server_socket.bind((host, port))
    server_socket.listen(5)
    logging.debug('started listening')
    server_socket.setblocking(0)
    
    while not stop_threads:
        r, w, e = select.select([server_socket], [], [], 0)
        for s in r:
            if s == server_socket:
                client_socket, client_address = server_socket.accept()
                
                # Validate host
                if not valid_host(client_address[0]):
                    logging.info('Access attempted from blocked host %s' % client_address[0])
                    client_socket.sendall(error403.encode())
                    client_socket.shutdown(socket.SHUT_RDWR)
                    client_socket.close()
                    continue
                
                # Receive request
                request = client_socket.recv(1024).decode()
                logging.info('Connection from ' + client_address[0])
                logging.debug('Parsing request')
                
                # Parse request
                for line in request.splitlines():
                    prefix = line.split(' ', 1)[0]
                    if prefix in ('POST', 'HEAD', 'PUT', 'DELETE', 'OPTIONS', 'CONNECT'):
                        logging.debug('Sending Error 405')
                        client_socket.sendall(error405.encode())
                    elif prefix != 'GET':
                        pass
                    else:
                        # GET request
                        method, url, trailer = line.split()
                        url = url.split('?')[0]
                        
                        # Check for device reset URLs
                        device_reset_urls = [f'/reset_{name}' for name in DEVICES.keys()]
                        valid_urls = ['/', '/config', '/log'] + device_reset_urls
                        
                        if url not in valid_urls:
                            client_socket.sendall(error404.encode())
                        elif url == '/log':
                            # Show log file
                            logging.debug('Sending log file(%s)' % log_file)
                            reply = base_header + ok_header
                            try:
                                with open(log_file, 'r') as lf:
                                    reply += lf.read()
                            except IOError as e:
                                reply += str(e)
                            client_socket.sendall(reply.encode())
                        elif url == '/config':
                            # Show config
                            reply = base_header + ok_header
                            current_config = io.StringIO()
                            config.write(current_config)
                            reply += current_config.getvalue()
                            current_config.close()
                            logging.debug('Sending config')
                            client_socket.sendall(reply.encode())
                        elif url == '/':
                            # Main page with all devices
                            reply = base_header + ok_header + html_header + clacks_header + refresh_header + end_header
                            reply += '<h1>FakeWake - Multi Device Control</h1>'
                            reply += '<b>Pingable:</b> %s<br><hr>' % PINGABLE
                            
                            # Add controls for each device
                            for device_name, device in DEVICES.items():
                                current_time = time.time()
                                device_last_action = last_action_time.get(device_name, 0)
                                
                                if current_time > device_last_action + MIN_INTERVAL:
                                    button_state = ''
                                else:
                                    button_state = 'disabled'
                                
                                reply += f'<h3>Device: {device_name}</h3>'
                                reply += f'<p>GPIO Pin: {device.reset_gpio}</p>'
                                reply += f'<form action="/reset_{device_name}" method="get">'
                                reply += f'<input type="submit" value="Reset {device_name}" {button_state}></form><br>'
                            
                            reply += '</body></html>'
                            client_socket.sendall(reply.encode())
                        elif url.startswith('/reset_'):
                            # Device reset action
                            device_name = url[7:]  # Remove '/reset_' prefix
                            if device_name in DEVICES:
                                reply = base_header + ok_header + html_header + clacks_header + refresh_header
                                reply += f'<p>Resetting {device_name}. Please wait...</p>'
                                reply += '<center><form action="/" method="get">'
                                reply += '<input type="submit" value="Continue">'
                                reply += '</form></center></body></html>'
                                client_socket.sendall(reply.encode())
                                
                                logging.debug(f'Resetting device {device_name}')
                                reset_device(device_name, SHORT_PRESS)
                            else:
                                client_socket.sendall(error404.encode())
                
                client_socket.close()
        
        # Reduce CPU load
        time.sleep(0.1)
    
    server_socket.close()
    logging.debug('Webserver exiting')

def start_pinger():
    """Start the pinger thread"""
    global PINGABLE
    PINGABLE = 'Unknown'
    if PINGER_ENABLED and TARGET_IDS and any(t.strip() for t in TARGET_IDS):
        logging.debug('Starting Pinger')
        ping_thread = threading.Thread(name='pinger',
                                       target=pinger,
                                       args=(TARGET_IDS, PING_INTERVAL))
        ping_thread.start()
    else:
        ping_thread = None
    return ping_thread

def start_webserver():
    """Start the webserver thread"""
    if WEBSERVER_ENABLED:
        logging.debug('Starting Webserver')
        webserver_thread = threading.Thread(name='webserver',
                                            target=webserver,
                                            args=(WEBSERVER_HOST, WEBSERVER_PORT))
        webserver_thread.start()
    else:
        webserver_thread = None
    return webserver_thread

## Main execution
if __name__ == '__main__':
    
    # Default config
    default_config = {
        'short': '0.5',
        'min_interval': '30',
        'web_enabled': 'True',
        'host': '',
        'web_port': '8080',
        'reload_delay': '15',
        'ping_enabled': 'True',
        'target': '',
        'interval': '1',
        'restart': 'True',
        'hosts_allow': '',
        'hosts_deny': '',
        'drop_privs': 'True',
        'user': 'nobody',
        # Device configuration - example format
        'device1_name': 'Jetson1',
        'device1_gpio': '24',
        'device2_name': 'Jetson2', 
        'device2_gpio': '25',
    }
    
    # Global variables
    PINGABLE = 'Unknown'
    
    # Command line arguments
    cmd_parser = argparse.ArgumentParser(
        description='Multi-device reset control daemon via GPIO.',
        epilog='If -c is not specified, internal defaults will be used'
    )
    cmd_parser.add_argument('--debug', help='Enable debug output to logfile',
                            action='store_true', default=False)
    cmd_parser.add_argument('-c', '--config', help='Load config from specified file.',
                            default='')
    cmd_parser.add_argument('-N', '--nodaemon',
                            help='Do not daemonize. Run in foreground instead.',
                            action='store_true', default=False)
    cmd_args = cmd_parser.parse_args()
    
    if cmd_args.config:
        cmd_args.config = os.path.abspath(cmd_args.config)
    
    # Daemonize
    if not cmd_args.nodaemon:
        _daemonize_me()
    
    # Set up logging
    log_file = '/tmp/fakewake.log'
    log_format = '%(asctime)s:%(levelname)s:%(threadName)s:%(message)s'
    log_filemode = 'w'
    
    # Manage log files
    try:
        os.rename(log_file + '2', log_file + '3')
    except OSError:
        pass
    try:
        os.rename(log_file, log_file + '2')
    except OSError:
        pass
    
    # Base logger
    log_level = logging.DEBUG if cmd_args.debug else logging.INFO
    logging.basicConfig(level=log_level,
                        filename=log_file, filemode=log_filemode,
                        format=log_format)
    
    # Set permissions on log file
    try:
        os.chmod(log_file, 0o666)
    except OSError:
        pass
    
    # Log errors and warnings to stderr
    stderr_logger = logging.StreamHandler(sys.stderr)
    stderr_formatter = logging.Formatter('fakewake:%(levelname)s:%(message)s')
    stderr_logger.setLevel(logging.WARNING)
    stderr_logger.setFormatter(stderr_formatter)
    logging.getLogger('').addHandler(stderr_logger)
    
    # Console logger (uncomment for console output)
    console_logger = logging.StreamHandler()
    console_formatter = logging.Formatter(log_format)
    console_logger.setFormatter(console_formatter)
    logging.getLogger('').addHandler(console_logger)
    
    logging.info('\x1b[2J\x1b[H')  # Clear screen hack
    logging.info('My pid: %s' % os.getpid())
    
    # Read config
    logging.debug('Reading config file')
    config = configparser.ConfigParser()
    
    # Set defaults
    config.read_dict({'DEFAULT': default_config})
    
    try:
        if cmd_args.config:
            config.read(cmd_args.config)
    except configparser.Error as e:
        msg = f'Error parsing config file {cmd_args.config}: {str(e)}'
        logging.critical(msg)
        sys.exit(msg)
    
    # Parse configuration
    try:
        SHORT_PRESS = config.getfloat('DEFAULT', 'short')
        MIN_INTERVAL = config.getfloat('DEFAULT', 'min_interval')
    except (configparser.NoSectionError, ValueError) as e:
        logging.error(f"Error reading timing config: {e}")
        SHORT_PRESS = float(default_config['short'])
        MIN_INTERVAL = float(default_config['min_interval'])
    
    try:
        WEBSERVER_ENABLED = config.getboolean('DEFAULT', 'web_enabled')
        WEBSERVER_HOST = config.get('DEFAULT', 'host')
        WEBSERVER_PORT = config.getint('DEFAULT', 'web_port')
        WEBSERVER_RELOAD_DELAY = config.get('DEFAULT', 'reload_delay')
    except (configparser.NoSectionError, ValueError) as e:
        logging.error(f"Error reading webserver config: {e}")
        WEBSERVER_ENABLED = True
        WEBSERVER_HOST = ''
        WEBSERVER_PORT = 8080
        WEBSERVER_RELOAD_DELAY = '15'
    
    try:
        PINGER_ENABLED = config.getboolean('DEFAULT', 'ping_enabled')
        TARGET_IDS = [t.strip() for t in config.get('DEFAULT', 'target').split(',') if t.strip()]
        PING_INTERVAL = config.getfloat('DEFAULT', 'interval')
    except (configparser.NoSectionError, ValueError) as e:
        logging.error(f"Error reading pinger config: {e}")
        PINGER_ENABLED = False
        TARGET_IDS = []
        PING_INTERVAL = 1.0
    
    try:
        RESTART_THREADS = config.getboolean('DEFAULT', 'restart')
        DROP_PRIVS = config.getboolean('DEFAULT', 'drop_privs')
        PRIVS_NAME = config.get('DEFAULT', 'user')
    except (configparser.NoSectionError, ValueError) as e:
        logging.error(f"Error reading security config: {e}")
        RESTART_THREADS = True
        DROP_PRIVS = True
        PRIVS_NAME = 'nobody'
    
    try:
        RAW_HOSTS_ALLOW = config.get('DEFAULT', 'hosts_allow')
        RAW_HOSTS_DENY = config.get('DEFAULT', 'hosts_deny')
    except configparser.NoSectionError as e:
        logging.error(f"Error reading hosts config: {e}")
        RAW_HOSTS_ALLOW = ''
        RAW_HOSTS_DENY = ''
    
        # Parse host lists
        HOSTS_ALLOW = [t.strip() for t in RAW_HOSTS_ALLOW.split(',') if t.strip()]
        HOSTS_DENY = [t.strip() for t in RAW_HOSTS_DENY.split(',') if t.strip()]
    
        # Initialize devices from config
        logging.debug('Initializing devices')
        device_count = 1
        while True:
            name_key = f'device{device_count}_name'
            gpio_key = f'device{device_count}_gpio'
            
            try:
                device_name = config.get('DEFAULT', name_key)
                device_gpio = config.getint('DEFAULT', gpio_key)
                
                logging.info(f'Adding device: {device_name} on GPIO {device_gpio}')
                DEVICES[device_name] = JetsonDevice(device_name, device_gpio)
                device_count += 1
            except (configparser.NoOptionError, ValueError):
                break
        
        if not DEVICES:
            msg = "No devices configured. Please check your configuration."
            logging.critical(msg)
            sys.exit(msg)
        
        logging.info(f'Initialized {len(DEVICES)} devices: {list(DEVICES.keys())}')
    
    try:
        # Initialize timing
        last_action_time = {}
        for device_name in DEVICES.keys():
            last_action_time[device_name] = time.time() - MIN_INTERVAL
        
        # Start threads
        stop_threads = False
        ping_thread = start_pinger()
        webserver_thread = start_webserver()
        
        # Determine local power control capability
        try:
            reboot_check = subprocess.run(['sudo', '-n', 'reboot', '-w'], 
                                        capture_output=True, text=True, timeout=5)
            poweroff_check = subprocess.run(['sudo', '-n', 'poweroff', '-w'], 
                                          capture_output=True, text=True, timeout=5)
            LOCAL_PWR_CTRL = (reboot_check.returncode == 0 and poweroff_check.returncode == 0)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            LOCAL_PWR_CTRL = False
        
        logging.debug('Local power control: %s' % LOCAL_PWR_CTRL)
            logging.debug('Attempting to drop root privileges')
            try:
                PRIVS_GROUP = grp.getgrgid(pwd.getpwnam(PRIVS_NAME)[3])[0]
                target_uid = pwd.getpwnam(PRIVS_NAME)[2]
                target_gid = grp.getgrnam(PRIVS_GROUP)[2]
                current_uid = os.getuid()
                
                if current_uid == 0:
                    logging.debug('Running as root. Attempting to drop privileges')
                    try:
                        os.chown(log_file, target_uid, target_gid)
                    except OSError:
                        logging.debug(f'Unable to change ownership of log file to {PRIVS_NAME}:{PRIVS_GROUP}')
                    
                    os.setgid(target_gid)
                    os.setuid(target_uid)
                    os.umask(0o77)
                    logging.debug(f'Success. Now running as {PRIVS_NAME}:{PRIVS_GROUP}')
                else:
                    current_uname = pwd.getpwuid(current_uid)[0]
                    current_gid = os.getgid()
                    current_gname = grp.getgrgid(current_gid)[0]
                    logging.debug(f'Currently running as user {current_uname}:{current_gname}. No need to drop privileges')
            except Exception as e:
                logging.warning(f'Failed to drop root privileges: {e}')
        
        # Drop root privileges
        if DROP_PRIVS:
        loop_delay = 0.5
        logging.debug('Entering main loop')
        
        while True:
            # Check for dead threads and restart
            if ping_thread is not None and not ping_thread.is_alive():
                if RESTART_THREADS:
                    ping_thread = start_pinger()
                else:
                    logging.warning('Pinger thread has died.')
                    PINGABLE = 'Unknown'
            
            if webserver_thread is not None and not webserver_thread.is_alive():
                if RESTART_THREADS:
                    webserver_thread = start_webserver()
                else:
                    logging.critical('Webserver thread has died. Exiting')
                    break
            
            time.sleep(loop_delay)
    
    except KeyboardInterrupt:
        logging.info('Received interrupt signal')
    
    finally:
        # Cleanup
        logging.debug('Cleaning up')
        logging.debug('Stopping threads')
        stop_threads = True
        
        logging.debug('Freeing GPIO')
        for device in DEVICES.values():
            try:
                device.cleanup()
            except Exception as e:
                logging.error(f'Error cleaning up device {device.name}: {e}')
        
        logging.info('Shutdown complete')