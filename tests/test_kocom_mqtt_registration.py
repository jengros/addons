"""Exercise Kocom methods without importing or starting the controller."""
import ast
import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kocomRS485" / "rs485.py").read_text(encoding="utf-8")
BASE_COMMIT = "c286eb1125e68cac68c9edb51def8e0088531273"
BASELINE = subprocess.check_output(
    ["git", "show", BASE_COMMIT + ":kocomRS485/rs485.py"], cwd=ROOT).decode("utf-8")


class Broker:
    def __init__(self):
        self.messages = []
        self.subscriptions = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload, kwargs))

    def subscribe(self, topics):
        self.subscriptions.extend(topics)


def controller(source=SOURCE):
    tree = ast.parse(source)
    env = {"json": json, "time": SimpleNamespace(time=lambda: 1000.0, sleep=lambda _: None),
           "logger": SimpleNamespace(info=lambda *_: None, debug=lambda *_: None)}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                if isinstance(node.value, ast.Dict) and all(
                        isinstance(n, (ast.Dict, ast.Name, ast.Constant, ast.Load))
                        for n in ast.walk(node.value)):
                    value = eval(compile(ast.Expression(node.value), "<constant mapping>", "eval"), env)
                else:
                    continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = value
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Kocom")
    names = {"on_message", "parse_message", "homeassistant_device_discovery",
             "send_to_homeassistant", "scan_list"}
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    # Limit the actual scanning loop to one cycle; expose its swallowed exception.
    scan = next(n for n in selected if n.name == "scan_list")
    loop = scan.body[0]
    scan.body[0] = ast.For(target=ast.Name(id="_cycle", ctx=ast.Store()),
                          iter=ast.Call(func=ast.Name(id="range", ctx=ast.Load()),
                                        args=[ast.Constant(1)], keywords=[]),
                          body=loop.body, orelse=[])
    for node in ast.walk(scan):
        if isinstance(node, ast.ExceptHandler):
            node.body = [ast.Raise()]
    isolated = ast.Module(body=[ast.ClassDef(name="Probe", bases=[], keywords=[],
                                           body=selected, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, "<isolated deployed methods>", "exec"), env)
    obj = env["Probe"]()
    obj._name = "kocom"
    obj.d_mqtt = Broker()
    obj.ha_registry = False
    obj.kocom_scan = True
    obj.connected = True
    obj.tick = 0
    obj.wp_light = obj.wp_fan = obj.wp_thermostat = obj.wp_elevator = True
    obj.wp_gas = obj.wp_plug = False

    def state(value):
        return {"state": value, "set": value, "last": "state", "count": 0}

    def scan_state():
        return {"tick": 0, "last": 0, "count": 0}

    obj.wp_list = {
        "light": {"livingroom": {"scan": scan_state(),
                                **{"light" + str(i): state("off") for i in range(4)}}},
        "thermostat": {room: {"scan": scan_state(), "mode": state("off"),
                              "current_temp": state(26), "target_temp": state(22)}
                       for room in ("livingroom", "bedroom", "room1", "room2")},
        "fan": {"wallpad": {"scan": scan_state(), "mode": state("off"), "speed": state("off")}},
        "elevator": {"wallpad": {"scan": scan_state(), "elevator": state("off")}},
        "gas": {"wallpad": {"scan": scan_state(), "gas": state("off")}},
        "plug": {"livingroom": {"scan": scan_state(), "plug0": state("on")}},
    }
    obj.transmissions = []
    obj.set_serial = lambda *a, **kw: obj.transmissions.append((a, kw))
    obj.packet_parsing = lambda *a, **kw: obj.transmissions.append((a, kw))
    return obj


def deliver(obj, topic, payload=b"{}", retained=False):
    obj.on_message(None, None, SimpleNamespace(topic=topic, payload=payload, retain=retained))


def registration_echoes(obj):
    messages = list(obj.d_mqtt.messages)
    for topic, payload, _ in messages:
        deliver(obj, topic, payload.encode(), retained=True)
    # On reconnect, retained messages and freshly published echoes can both arrive.
    for topic, payload, _ in messages:
        deliver(obj, topic, payload.encode(), retained=False)


class RegistrationTests(unittest.TestCase):
    def test_baseline_reproduces_scan_failure(self):
        obj = controller(BASELINE)
        obj.homeassistant_device_discovery(initial=True)
        registration_echoes(obj)
        self.assertEqual(obj.wp_list["light"]["livingroom"]["scan"]["last"], "config")
        with self.assertRaises(TypeError):
            obj.scan_list()

    def test_reconnect_duplicates_preserve_state_and_resume_scan(self):
        obj = controller()
        original = copy.deepcopy(obj.wp_list)
        obj.homeassistant_device_discovery(initial=True)
        registration_echoes(obj)
        self.assertEqual(obj.wp_list, original)
        self.assertFalse(obj.kocom_scan)
        obj.scan_list()
        self.assertEqual(len(obj.transmissions), 6)  # light, fan, four thermostats

    def test_only_last_registration_echo_opens_gate(self):
        obj = controller()
        obj.homeassistant_device_discovery(initial=True)
        deliver(obj, "homeassistant/light/livingroom_scan/config")
        self.assertTrue(obj.kocom_scan)
        obj.scan_list()
        self.assertEqual(obj.transmissions, [])
        deliver(obj, obj.ha_registry)
        self.assertFalse(obj.kocom_scan)

    def test_no_registry_does_not_open_gate(self):
        obj = controller()
        original = copy.deepcopy(obj.wp_list)
        deliver(obj, "homeassistant/light/livingroom_scan/config")
        self.assertTrue(obj.kocom_scan)
        self.assertEqual(obj.wp_list, original)

    def test_empty_deleted_or_invalid_payloads_are_not_commands(self):
        obj = controller()
        obj.kocom_scan = False
        original = copy.deepcopy(obj.wp_list)
        for payload in (b"", b"{}", b"null", b"broken-json", b"\xff"):
            for retained in (False, True):
                deliver(obj, "homeassistant/light/livingroom_scan/config", payload, retained)
        self.assertEqual(obj.wp_list, original)
        self.assertEqual(obj.d_mqtt.messages, [])

    def test_birth_reregistration_and_echoes(self):
        obj = controller()
        obj.homeassistant_device_discovery(initial=True)
        registration_echoes(obj)
        original = copy.deepcopy(obj.wp_list)
        for _ in range(2):
            obj.d_mqtt.messages.clear()
            deliver(obj, "homeassistant/status", b"online")
            self.assertTrue(obj.kocom_scan)
            registration_echoes(obj)
            self.assertFalse(obj.kocom_scan)
        self.assertEqual(obj.wp_list, original)

    def test_non_online_birth_is_ignored(self):
        obj = controller()
        for payload in (b"offline", b"", b"ONLINE", b"\xff"):
            deliver(obj, "homeassistant/status", payload)
        self.assertEqual(obj.d_mqtt.messages, [])

    def test_light_command_after_echoes_reaches_send_loop(self):
        obj = controller()
        obj.homeassistant_device_discovery(initial=True)
        registration_echoes(obj)
        for domain in ("light", "thermostat", "fan"):
            for room in obj.wp_list[domain].values():
                room["scan"]["tick"] = 1000
        deliver(obj, "homeassistant/light/livingroom_light1/set", b"on")
        obj.scan_list()
        self.assertIn((("light", "livingroom", "light1", "on"), {}), obj.transmissions)

    def test_normal_commands_match_baseline(self):
        for topic, payload in [("homeassistant/light/livingroom_light1/set", b"on"),
                               ("homeassistant/climate/bedroom/mode", b"heat"),
                               ("homeassistant/climate/bedroom/target_temp", b"24"),
                               ("homeassistant/fan/wallpad/mode", b"on"),
                               ("homeassistant/fan/wallpad/speed", b"low"),
                               ("homeassistant/switch/wallpad_elevator/set", b"on")]:
            with self.subTest(topic=topic):
                before, after = controller(BASELINE), controller()
                before.kocom_scan = after.kocom_scan = False
                deliver(before, topic, payload)
                deliver(after, topic, payload)
                self.assertEqual(before.wp_list, after.wp_list)
                self.assertEqual(before.d_mqtt.messages, after.d_mqtt.messages)

    def test_bridge_scan_and_registration_commands_preserved(self):
        obj = controller()
        obj.homeassistant_device_discovery(initial=True)
        registration_echoes(obj)
        deliver(obj, "rs485/bridge/config/scan", b"on")
        self.assertEqual(obj.wp_list["light"]["livingroom"]["scan"]["last"], 0)
        obj.d_mqtt.messages.clear()
        deliver(obj, "rs485/bridge/config/restart", b"on")
        self.assertGreater(len(obj.d_mqtt.messages), 0)
        self.assertTrue(obj.kocom_scan)

    def test_discovery_payloads_and_retained_removal_unchanged(self):
        for remove in (False, True):
            before, after = controller(BASELINE), controller()
            before.homeassistant_device_discovery(initial=True, remove=remove)
            after.homeassistant_device_discovery(initial=True, remove=remove)
            self.assertEqual(before.d_mqtt.messages, after.d_mqtt.messages)
            self.assertEqual(before.d_mqtt.subscriptions, after.d_mqtt.subscriptions)
            self.assertTrue(all(opts == {"retain": True} for _, _, opts in after.d_mqtt.messages))

    def test_other_methods_are_unchanged(self):
        def stable(source):
            tree = ast.parse(source)
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef) and cls.name == "Kocom":
                    cls.body = [n for n in cls.body if not
                                (isinstance(n, ast.FunctionDef) and n.name == "on_message")]
            return ast.dump(tree)
        self.assertEqual(stable(BASELINE), stable(SOURCE))
        compile(SOURCE, "rs485.py", "exec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
