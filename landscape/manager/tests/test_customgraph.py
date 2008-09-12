import os
import pwd

from twisted.internet.error import ProcessDone
from twisted.python.failure import Failure

from landscape.manager.customgraph import CustomGraphManager

from landscape.tests.helpers import (
    LandscapeTest, LandscapeIsolatedTest, ManagerHelper, StubProcessFactory)


class StubManagerStore(object):

    def __init__(self):
        self.graphes = {}

    def add_graph(self, graph_id, filename, user):
        self.graphes[graph_id] = (filename, user)

    def get_graphes(self):
        for graph_id, (filename, user) in self.graphes.items():
            yield graph_id, filename, user

    def get_graph(self, graph_id):
        graph = self.graphes.get(graph_id)
        if graph:
            return graph_id, graph[0], graph[1]

    def remove_graph(self, graph_id):
        self.graphes.pop(graph_id, None)


class CustomGraphManagerTests(LandscapeIsolatedTest):
    helpers = [ManagerHelper]

    def setUp(self):
        super(CustomGraphManagerTests, self).setUp()
        self.store = StubManagerStore()
        self.manager.store = self.store
        self.broker_service.message_store.set_accepted_types(
            ["operation-result", "custom-graph"])
        self.data_path = self.make_dir()
        self.manager.config.data_path = self.data_path
        self.manager.config.script_users = "ALL"
        self.graph_manager = CustomGraphManager(
            create_time=range(5, 0, -1).pop)
        self.manager.add(self.graph_manager)

    def test_add_graph(self):
        uid = os.getuid()
        info = pwd.getpwuid(uid)
        username = info.pw_name
        result = self.manager.dispatch_message(
            {"type": "custom-graph-add",
                     "interpreter": "/bin/sh",
                     "code": "echo hi!",
                     "username": username,
                     "operation-id": 456,
                     "graph-id": 123})
        def got_result(r):
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [{"api": "3.1",
                  "operation-id": 456,
                  "result-text": u"",
                  "status": 6,
                  "timestamp": 0,
                  "type": "operation-result"}])
            self.assertEquals(
                self.store.graphes,
                {123: (os.path.join(self.data_path, "custom-graph-scripts",
                                    "graph-123"),
                       username)})
        result.addCallback(got_result)
        return result

    def test_remove_unknown_graph(self):
        self.manager.dispatch_message(
            {"type": "custom-graph-remove",
                     "operation-id": 456,
                     "graph-id": 123})

    def test_remove_graph(self):
        filename = self.makeFile()
        tempfile = file(filename, "w")
        tempfile.write("foo")
        tempfile.close()
        self.store.add_graph(123, filename, u"user")
        self.manager.dispatch_message(
            {"type": "custom-graph-remove",
                     "operation-id": 456,
                     "graph-id": 123})
        self.assertFalse(os.path.exists(filename))

    def test_run(self):
        uid = os.getuid()
        info = pwd.getpwuid(uid)
        username = info.pw_name
        filename = self.makeFile()
        tempfile = file(filename, "w")
        tempfile.write("#!/bin/sh\necho 1")
        tempfile.close()
        os.chmod(filename, 0777)
        self.store.add_graph(123, filename, None)
        def check(ignore):
            self.graph_manager.exchange()
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [{"api": "3.1",
                  "data": {123: {"error": u"", "values": [(1, 1.0)]}},
                  "timestamp": 0,
                  "type": "custom-graph"}])
        return self.graph_manager.run().addCallback(check)

    def test_run_cast_result_error(self):
        self.store.add_graph(123, "foo", None)
        factory = StubProcessFactory()
        self.graph_manager.process_factory = factory
        result = self.graph_manager.run()

        self.assertEquals(len(factory.spawns), 1)
        spawn = factory.spawns[0]
        self.assertEquals(spawn[1], "foo")

        protocol = spawn[0]
        protocol.childDataReceived(1, "foobar")
        for fd in (0, 1, 2):
            protocol.childConnectionLost(fd)
        protocol.processEnded(Failure(ProcessDone(0)))
        
        def check(ignore):
            self.graph_manager.exchange()
            self.assertMessages(
                self.broker_service.message_store.get_pending_messages(),
                [{"api": "3.1",
                  "data":
                      {123: {"error":
                          u"ValueError: invalid literal for float(): foobar",
                             "values": []}},
                  "timestamp": 0,
                  "type": "custom-graph"}])
        return result.addCallback(check)
