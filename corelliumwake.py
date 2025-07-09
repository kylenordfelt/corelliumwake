#!/usr/bin/env python3
"""
Jetson Reset Controller
Modified from fakewake to control 5 Jetson Orin devices independently
Uses transistors for reset control instead of analog switches
"""

import sys
import os
import time
import socket
import struct
import threading
import logging
import configparser
import argparse
from datetime import datetime
try:
    from gpiozero import OutputDevice
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs
except ImportError as e:
    print(f"Required module not found: {e}")
    print("Install with: pip3 install gpiozero")
    sys.exit(1)

class JetsonResetController:
    def __init__(self, config_file='jetson_reset.cfg'):
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        self.load_config()
        
        # Initialize logging
        self.setup_logging()
        
        # Initialize GPIO devices for each Jetson
        self.reset_devices = {}
        self.init_gpio()
        
        # Web server
        self.web_server = None
        self.web_thread = None
        
        # UDP listeners for magic packets
        self.udp_threads = []
        
        self.logger.info("Jetson Reset Controller initialized")
    
    def load_config(self):
        """Load configuration from file"""
        if not os.path.exists(self.config_file):
            self.create_default_config()
        
        self.config.read(self.config_file)
        
        # Validate required sections
        required_sections = ['general', 'web', 'udp']
        for section in required_sections:
            if not self.config.has_section(section):
                self.config.add_section(section)
        
        # Add jetson sections if they don't exist
        for i in range(1, 6):
            section = f'jetson{i}'
            if not self.config.has_section(section):
                self.config.add_section(section)
    
    def create_default_config(self):
        """Create a default configuration file"""
        config = configparser.ConfigParser()
        
        config.add_section('general')
        config.set('general', 'log_level', 'INFO')
        config.set('general', 'reset_pulse_duration', '0.5')
        
        config.add_section('web')
        config.set('web', 'enabled', 'True')
        config.set('web', 'port', '8080')
        config.set('web', 'bind_address', '0.0.0.0')
        
        config.add_section('udp')
        config.set('udp', 'enabled', 'True')
        config.set('udp', 'port', '9')
        config.set('udp', 'bind_address', '0.0.0.0')
        
        # Default GPIO pins and names for 5 Jetsons
        jetson_configs = [
            ('jetson1', 'Jetson-Orin-1', '18'),
            ('jetson2', 'Jetson-Orin-2', '19'),
            ('jetson3', 'Jetson-Orin-3', '20'),
            ('jetson4', 'Jetson-Orin-4', '21'),
            ('jetson5', 'Jetson-Orin-5', '26'),
        ]
        
        for section, name, gpio in jetson_configs:
            config.add_section(section)
            config.set(section, 'name', name)
            config.set(section, 'gpio_pin', gpio)
            config.set(section, 'enabled', 'True')
            config.set(section, 'magic_packet_mac', f'00:00:00:00:00:0{section[-1]}')
        
        with open(self.config_file, 'w') as f:
            config.write(f)
        
        print(f"Created default config file: {self.config_file}")
    
    def setup_logging(self):
        """Setup logging configuration"""
        log_level = self.config.get('general', 'log_level', fallback='INFO')
        
        # Setup file logging
        log_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        file_handler = logging.FileHandler('/tmp/jetson_reset.log')
        file_handler.setFormatter(log_formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        
        self.logger = logging.getLogger('JetsonResetController')
        self.logger.setLevel(getattr(logging, log_level.upper()))
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def init_gpio(self):
        """Initialize GPIO devices for each Jetson"""
        self.reset_pulse_duration = float(self.config.get('general', 'reset_pulse_duration', fallback='0.5'))
        
        for i in range(1, 6):
            section = f'jetson{i}'
            if self.config.getboolean(section, 'enabled', fallback=True):
                gpio_pin = int(self.config.get(section, 'gpio_pin'))
                name = self.config.get(section, 'name', fallback=f'Jetson-{i}')
                
                try:
                    # Create OutputDevice for reset control
                    # Initial state is False (transistor off, reset line not pulled low)
                    reset_device = OutputDevice(gpio_pin, initial_value=False)
                    self.reset_devices[i] = {
                        'device': reset_device,
                        'name': name,
                        'gpio_pin': gpio_pin
                    }
                    self.logger.info(f"Initialized {name} on GPIO {gpio_pin}")
                except Exception as e:
                    self.logger.error(f"Failed to initialize {name} on GPIO {gpio_pin}: {e}")
    
    def reset_jetson(self, jetson_id):
        """Reset a specific Jetson device"""
        if jetson_id not in self.reset_devices:
            self.logger.error(f"Jetson ID {jetson_id} not found or not enabled")
            return False
        
        jetson = self.reset_devices[jetson_id]
        
        try:
            self.logger.info(f"Resetting {jetson['name']} (GPIO {jetson['gpio_pin']})")
            
            # Pull reset line low (turn on transistor)
            jetson['device'].on()
            
            # Hold for configured duration
            time.sleep(self.reset_pulse_duration)
            
            # Release reset line (turn off transistor)
            jetson['device'].off()
            
            self.logger.info(f"Reset complete for {jetson['name']}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to reset {jetson['name']}: {e}")
            return False
    
    def reset_all_jetsons(self):
        """Reset all enabled Jetson devices"""
        self.logger.info("Resetting all Jetson devices")
        results = {}
        
        for jetson_id in self.reset_devices:
            results[jetson_id] = self.reset_jetson(jetson_id)
        
        return results
    
    def start_web_server(self):
        """Start the web server for HTTP control"""
        if not self.config.getboolean('web', 'enabled', fallback=True):
            return
        
        port = int(self.config.get('web', 'port', fallback='8080'))
        bind_address = self.config.get('web', 'bind_address', fallback='0.0.0.0')
        
        class RequestHandler(BaseHTTPRequestHandler):
            def __init__(self, controller, *args, **kwargs):
                self.controller = controller
                super().__init__(*args, **kwargs)
            
            def do_GET(self):
                parsed_url = urlparse(self.path)
                query_params = parse_qs(parsed_url.query)
                
                if parsed_url.path == '/':
                    self.send_main_page()
                elif parsed_url.path == '/reset':
                    jetson_id = query_params.get('jetson', [None])[0]
                    if jetson_id == 'all':
                        results = self.controller.reset_all_jetsons()
                        self.send_reset_response(results)
                    elif jetson_id and jetson_id.isdigit():
                        jetson_id = int(jetson_id)
                        result = self.controller.reset_jetson(jetson_id)
                        self.send_reset_response({jetson_id: result})
                    else:
                        self.send_error(400, "Invalid jetson parameter")
                elif parsed_url.path == '/status':
                    self.send_status_page()
                elif parsed_url.path == '/config':
                    self.send_config_page()
                else:
                    self.send_error(404, "Not Found")
            
            def send_main_page(self):
                html = self.generate_main_page()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(html.encode())
            
            def send_reset_response(self, results):
                html = self.generate_reset_response(results)
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(html.encode())
            
            def send_status_page(self):
                html = self.generate_status_page()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(html.encode())
            
            def send_config_page(self):
                config_text = self.controller.get_config_text()
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(config_text.encode())
            
            def generate_main_page(self):
                html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Corellium Reset Controller</title>
                    <style>
                        body { font-family: Arial, sans-serif; margin: 40px; }
                        .jetson-card { 
                            border: 1px solid #ddd; 
                            padding: 20px; 
                            margin: 10px 0; 
                            border-radius: 5px;
                            background-color: #f9f9f9;
                        }
                        .reset-btn { 
                            background-color: #ff4444; 
                            color: white; 
                            padding: 10px 20px; 
                            border: none; 
                            border-radius: 3px; 
                            cursor: pointer;
                            font-size: 16px;
                        }
                        .reset-btn:hover { background-color: #cc0000; }
                        .reset-all-btn { 
                            background-color: #ff8800; 
                            color: white; 
                            padding: 15px 30px; 
                            border: none; 
                            border-radius: 5px; 
                            cursor: pointer;
                            font-size: 18px;
                            margin: 20px 0;
                        }
                        .reset-all-btn:hover { background-color: #cc6600; }
                        .nav { margin-bottom: 20px; }
                        .nav a { margin-right: 15px; text-decoration: none; color: #0066cc; }
                    </style>
                </head>
                <body>
                    <h1>Corellium Reset Controller</h1>
                    <div class="nav">
                        <a href="/">Home</a>
                        <a href="/status">Status</a>
                        <a href="/config">Config</a>
                    </div>
                    
                    <button class="reset-all-btn" onclick="resetAll()">Reset All Corelliums</button>
                    
                    <h2>Individual Controls</h2>
                """
                
                for jetson_id, jetson in self.controller.reset_devices.items():
                    html += f"""
                    <div class="jetson-card">
                        <h3>{jetson['name']}</h3>
                        <p>GPIO Pin: {jetson['gpio_pin']}</p>
                        <button class="reset-btn" onclick="resetJetson({jetson_id})">Reset {jetson['name']}</button>
                    </div>
                    """
                
                html += """
                    <script>
                        function resetJetson(id) {
                            if (confirm('Are you sure you want to reset this Jetson?')) {
                                window.location.href = '/reset?jetson=' + id;
                            }
                        }
                        
                        function resetAll() {
                            if (confirm('Are you sure you want to reset ALL Jetsons?')) {
                                window.location.href = '/reset?jetson=all';
                            }
                        }
                    </script>
                </body>
                </html>
                """
                return html
            
            def generate_reset_response(self, results):
                html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Reset Results</title>
                    <style>
                        body { font-family: Arial, sans-serif; margin: 40px; }
                        .success { color: green; }
                        .error { color: red; }
                        .back-btn { 
                            background-color: #0066cc; 
                            color: white; 
                            padding: 10px 20px; 
                            border: none; 
                            border-radius: 3px; 
                            cursor: pointer;
                            margin-top: 20px;
                        }
                    </style>
                </head>
                <body>
                    <h1>Reset Results</h1>
                """
                
                for jetson_id, success in results.items():
                    jetson_name = self.controller.reset_devices[jetson_id]['name']
                    status_class = 'success' if success else 'error'
                    status_text = 'Success' if success else 'Failed'
                    html += f'<p class="{status_class}">{jetson_name}: {status_text}</p>'
                
                html += """
                    <button class="back-btn" onclick="window.location.href='/'">Back to Main</button>
                </body>
                </html>
                """
                return html
            
            def generate_status_page(self):
                html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>System Status</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; margin: 40px; }}
                        .status-table {{ border-collapse: collapse; width: 100%; }}
                        .status-table th, .status-table td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                        .status-table th {{ background-color: #f2f2f2; }}
                        .back-btn {{ 
                            background-color: #0066cc; 
                            color: white; 
                            padding: 10px 20px; 
                            border: none; 
                            border-radius: 3px; 
                            cursor: pointer;
                            margin-top: 20px;
                        }}
                    </style>
                </head>
                <body>
                    <h1>System Status</h1>
                    <p>Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                    
                    <h2>Jetson Devices</h2>
                    <table class="status-table">
                        <tr>
                            <th>ID</th>
                            <th>Name</th>
                            <th>GPIO Pin</th>
                            <th>Status</th>
                        </tr>
                """
                
                for jetson_id, jetson in self.controller.reset_devices.items():
                    html += f"""
                        <tr>
                            <td>{jetson_id}</td>
                            <td>{jetson['name']}</td>
                            <td>{jetson['gpio_pin']}</td>
                            <td>Ready</td>
                        </tr>
                    """
                
                html += """
                    </table>
                    <button class="back-btn" onclick="window.location.href='/'">Back to Main</button>
                </body>
                </html>
                """
                return html
            
            def log_message(self, format, *args):
                # Suppress default HTTP server logging
                pass
        
        # Create request handler with controller reference
        handler = lambda *args, **kwargs: RequestHandler(self, *args, **kwargs)
        
        try:
            self.web_server = HTTPServer((bind_address, port), handler)
            self.web_thread = threading.Thread(target=self.web_server.serve_forever)
            self.web_thread.daemon = True
            self.web_thread.start()
            self.logger.info(f"Web server started on {bind_address}:{port}")
        except Exception as e:
            self.logger.error(f"Failed to start web server: {e}")
    
    def start_udp_listener(self):
        """Start UDP listener for magic packets"""
        if not self.config.getboolean('udp', 'enabled', fallback=True):
            return
        
        port = int(self.config.get('udp', 'port', fallback='9'))
        bind_address = self.config.get('udp', 'bind_address', fallback='0.0.0.0')
        
        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            try:
                sock.bind((bind_address, port))
                self.logger.info(f"UDP listener started on {bind_address}:{port}")
                
                while True:
                    data, addr = sock.recvfrom(1024)
                    self.handle_magic_packet(data, addr)
                    
            except Exception as e:
                self.logger.error(f"UDP listener error: {e}")
            finally:
                sock.close()
        
        thread = threading.Thread(target=udp_listener)
        thread.daemon = True
        thread.start()
        self.udp_threads.append(thread)
    
    def handle_magic_packet(self, data, addr):
        """Handle received magic packet"""
        if len(data) < 102:  # Standard WOL packet is 102 bytes
            return
        
        # Check for magic packet header (6 bytes of 0xFF)
        if data[:6] != b'\xff' * 6:
            return
        
        # Extract MAC address from packet (next 6 bytes repeated 16 times)
        mac_bytes = data[6:12]
        mac_address = ':'.join(f'{b:02x}' for b in mac_bytes)
        
        self.logger.info(f"Received magic packet from {addr} for MAC {mac_address}")
        
        # Check which Jetson this MAC corresponds to
        for i in range(1, 6):
            section = f'jetson{i}'
            if self.config.has_section(section):
                config_mac = self.config.get(section, 'magic_packet_mac', fallback='')
                if config_mac.lower() == mac_address.lower():
                    self.logger.info(f"Magic packet matched {section}")
                    self.reset_jetson(i)
                    return
        
        self.logger.info(f"No matching Jetson found for MAC {mac_address}")
    
    def get_config_text(self):
        """Get configuration as text"""
        config_text = ""
        for section in self.config.sections():
            config_text += f"[{section}]\n"
            for key, value in self.config.items(section):
                config_text += f"{key} = {value}\n"
            config_text += "\n"
        return config_text
    
    def cleanup(self):
        """Clean up resources"""
        self.logger.info("Shutting down Jetson Reset Controller")
        
        # Close GPIO devices
        for jetson_id, jetson in self.reset_devices.items():
            try:
                jetson['device'].close()
            except:
                pass
        
        # Stop web server
        if self.web_server:
            self.web_server.shutdown()
    
    def run(self):
        """Main run loop"""
        try:
            self.start_web_server()
            self.start_udp_listener()
            
            self.logger.info("Jetson Reset Controller is running...")
            self.logger.info("Press Ctrl+C to stop")
            
            # Keep the main thread alive
            while True:
                time.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
        finally:
            self.cleanup()

def main():
    parser = argparse.ArgumentParser(description='Jetson Reset Controller')
    parser.add_argument('-c', '--config', default='jetson_reset.cfg',
                       help='Configuration file path')
    parser.add_argument('--create-config', action='store_true',
                       help='Create default configuration file and exit')
    
    args = parser.parse_args()
    
    if args.create_config:
        controller = JetsonResetController(args.config)
        print(f"Default configuration created: {args.config}")
        return
    
    controller = JetsonResetController(args.config)
    controller.run()

if __name__ == '__main__':
    main()
 
