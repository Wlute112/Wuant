"""Launcher contracts using service doubles; no network or trading processes."""
import json
import ast
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]

STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["EVENTS"], "a") as f:
    f.write(json.dumps(dict(name=name, args=args, cwd=os.getcwd(),
        pythonpath=os.environ.get("PYTHONPATH"), workdir=os.environ.get("QUANT_WORKDIR"),
        api=os.environ.get("VITE_API_URL"), pid=os.getpid())) + "\\n")
if name == "python":
    if "-" in args:
        sys.stdin.read()
        sys.exit(int(os.environ.get("PREFLIGHT_EXIT", "0")))
    if "uvicorn" in args or "http.server" in args:
        while True: time.sleep(0.1)
sys.exit(0)
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quant launcher ")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Wuant"
        self.root.mkdir()
        shutil.copy2(ROOT / "quant", self.root / "quant")
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "install-quant.sh", self.root / "scripts" / "install-quant.sh")
        (self.root / "web" / "node_modules").mkdir(parents=True)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("python", "npm", "curl", "redis-cli", "open"):
            path = self.bin / name
            path.write_text(STUB)
            path.chmod(0o755)
        self.link = self.bin / "quant"
        self.link.symlink_to(Path("..") / "Wuant" / "quant")
        self.events = self.base / "events.jsonl"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        QUANT_PYTHON=str(self.bin / "python"), EVENTS=str(self.events),
                        TMPDIR=str(self.base), QUANT_API_PORT="18080", QUANT_WEB_PORT="15173")
        self.env.pop("PYTHONPATH", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, **env):
        return subprocess.run([str(self.link), *args], cwd=self.base,
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=8)

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def install(self, *extra):
        return subprocess.run(["bash", str(self.root / "scripts" / "install-quant.sh"),
                               "--bin-dir", str(self.base / "installed bin"),
                               "--rc-file", str(self.base / "shell config"), *extra],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def test_installer_bare_command_spaces_and_idempotence(self):
        rc = self.base / "shell config"
        rc.write_text("# existing configuration\nexport QUANT_TEST_MARKER=preserved\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = rc.read_text()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(rc.read_text(), contents)
        self.assertTrue(contents.startswith("# existing configuration\nexport QUANT_TEST_MARKER=preserved\n"))
        installed = self.base / "installed bin" / "quant"
        self.assertEqual(installed.resolve(), self.root / "quant")
        env = dict(self.env, PATH=os.environ["PATH"] + ":/tmp/existing path with spaces")
        result = subprocess.run(["zsh", "-f", "-c", 'source "$1"; print -r -- "$PATH"; quant --help', "test", str(rc)],
                                cwd=self.base, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], str(installed.parent) + ":" + env["PATH"])
        self.assertIn("Usage:", result.stdout)

    def test_installer_conflicts_and_invalid_options(self):
        bin_dir = self.base / "installed bin"
        bin_dir.mkdir()
        target = bin_dir / "quant"
        target.write_text("keep existing command")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(target.read_text(), "keep existing command")
        self.assertFalse((self.base / "shell config").exists())
        for args in (("--bad",), ("--bin-dir",), ("--rc-file", "relative")):
            with self.subTest(args=args):
                self.assertNotEqual(self.install(*args).returncode, 0)

    def test_help_and_invalid_flags(self):
        self.assertEqual(self.run_cli("--help").returncode, 0)
        self.assertEqual(self.run_cli("--invalid").returncode, 2)

    def test_subcommands_forward_arguments_and_runtime_environment(self):
        for command, module in (("doctor", "quant.ops.doctor"), ("compute", "quant.run.compute"),
                                ("benchmark", "quant.scripts.benchmark_compute")):
            with self.subTest(command=command):
                result = self.run_cli(command, "--test-arg", "value with spaces")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.records()[-1]
                self.assertIn(module, record["args"])
                self.assertEqual(record["args"][-2:], ["--test-arg", "value with spaces"])
                runtime = str(self.root / ".quant-runtime")
                self.assertEqual(record["cwd"], runtime)
                self.assertEqual(record["workdir"], runtime)
                self.assertEqual(record["pythonpath"].split(os.pathsep)[0], runtime)
                self.assertNotIn("", record["pythonpath"].split(os.pathsep))
                self.assertEqual((Path(runtime) / "quant").resolve(), self.root)

    def test_conflicting_runtime_is_preserved(self):
        runtime = self.root / ".quant-runtime"
        runtime.mkdir()
        (runtime / "quant").write_text("keep me")
        result = self.run_cli("compute")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((runtime / "quant").read_text(), "keep me")

    def test_conflicting_runtime_symlink_is_preserved(self):
        runtime = self.root / ".quant-runtime"
        runtime.mkdir()
        target = self.base / "elsewhere"
        (runtime / "quant").symlink_to(target)
        self.assertNotEqual(self.run_cli("compute").returncode, 0)
        self.assertEqual(os.readlink(runtime / "quant"), str(target))

    def test_api_workdir_override_and_default(self):
        tree = ast.parse((ROOT / "api" / "jobs.py").read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "WORKDIR" for t in node.targets))
        expression = compile(ast.Expression(assignment.value), "jobs WORKDIR", "eval")
        from types import SimpleNamespace
        for env, expected in (({}, ROOT.parent), ({"QUANT_WORKDIR": str(self.root / ".quant-runtime")},
                                                  self.root / ".quant-runtime")):
            value = eval(expression, {"Path": Path, "os": SimpleNamespace(environ=env),
                                      "__file__": str(ROOT / "api" / "jobs.py")})
            self.assertEqual(value, expected)

    def test_startup_build_readiness_and_term_cleanup(self):
        proc = subprocess.Popen([str(self.link), "--no-browser"], cwd=self.base, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children = []
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                records = self.records()
                children = [r for r in records if r["name"] == "python" and
                            ("uvicorn" in r["args"] or "http.server" in r["args"])]
                if len(children) == 2 and len([r for r in records if r["name"] == "curl"]) >= 4:
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(len(children), 2, records)
            self.assertTrue(any(r["name"] == "redis-cli" for r in records))
            build = next(r for r in records if r["name"] == "npm" and r["args"] == ["run", "build"])
            self.assertEqual(build["api"], "http://127.0.0.1:18080")
            self.assertFalse(any(r["name"] == "open" for r in records))
            self.assertTrue(all(r["args"] == ["ping"] for r in records if r["name"] == "redis-cli"))
            web = next(r for r in children if "http.server" in r["args"])
            self.assertEqual(web["cwd"], str(self.root / "web"))
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("http://127.0.0.1:15173", stdout)
            for child in children:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child["pid"], 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            for child in children:
                try:
                    os.kill(child["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
