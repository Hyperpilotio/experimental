import SocketServer as socketserver
import json
from subprocess import Popen, PIPE
import os

SOCKET="/var/run/command.sock"

class CommandHandler(socketserver.StreamRequestHandler):
    def handle(self):
        data = self.request.recv(4096)
        payload = json.loads(data)
        command = payload['command']
        command_id = payload['id']
        stdout, stderr, exit_code = self.run_command(command)
        response = {
            "id": command_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code
        }
        print(response)
        self.request.sendall(json.dumps(response))

    def run_command(self, command):
        process = Popen(command, shell=True, executable="/bin/bash", stdout=PIPE, stderr=PIPE)
        stdout, stderr = process.communicate()
        return stdout, stderr, process.returncode

if __name__ == '__main__':
    try:
        os.unlink(SOCKET)
    except:
        pass

    server = socketserver.UnixStreamServer(SOCKET, CommandHandler)
    server.serve_forever()
