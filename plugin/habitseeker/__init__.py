import json
import threading

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pynicotine.pluginsystem import BasePlugin

class Plugin(BasePlugin):

    def loaded_notification(self):
        self.log("HabitSeeker successfully loaded.")

    def disable(self):
        self.log("HabitSeeker disabled.")