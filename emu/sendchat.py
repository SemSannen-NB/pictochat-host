# usage: python3 sendchat.py PORT "text"  -> sends a text chat message through the host's GUI port
# Needs Pillow for the text rendering: run it with the repository's venv (../.venv/bin/python sendchat.py ...).
import importlib.util, json, socket, sys
spec = importlib.util.spec_from_file_location("g", __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "..", "pictochat_gui.py"))
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
pixels = g.text_to_pixels(sys.argv[2])
if pixels is None:
    sys.exit("text rendering needs Pillow: run this with the venv, e.g. ../.venv/bin/python sendchat.py PORT \"text\"")
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
except OSError as e:
    sys.exit("no host on 127.0.0.1:%s (%s): start it with --gui-port %s" % (sys.argv[1], e, sys.argv[1]))
tok = __import__("os").environ.get("PICTOCHAT_TOKEN")      # host started with --gui-token: authenticate first
if tok:
    s.sendall((json.dumps({"cmd": "auth", "token": tok}) + "\n").encode())
s.sendall((json.dumps({"cmd": "chat", "pixels": pixels}) + "\n").encode()); s.close()
print("sent")
