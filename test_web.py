import logging
import threading
import http.server
import socketserver

logging.basicConfig(level=logging.DEBUG)

def webserver(host, port):
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer((host, port), handler) as httpd:
        logging.info(f"Web server started at http://{host}:{port}")
        httpd.serve_forever()

logging.debug("Starting test web server thread")
t = threading.Thread(target=webserver, args=("0.0.0.0", 8080))
t.start()
