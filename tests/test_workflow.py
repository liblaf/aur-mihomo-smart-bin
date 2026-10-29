"""Execute publication workflow shell safeguards against an offline GitHub API."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/update.yml"


def step_script(name):
    """Read a named literal run block without requiring a YAML dependency."""
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      - name: {name}") + 1
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("      - "):
            break
        if line == "        run: |":
            body = []
            for command in lines[index + 1:]:
                if command and not command.startswith("          "):
                    break
                body.append(command[10:])
            return "\n".join(body)
    raise AssertionError(f"No literal shell script for {name}")


class WorkflowPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls.jsonl"
        gh = self.directory / "gh"
        gh.write_text("#!/usr/bin/env python3\n" + textwrap.dedent('''\
            import json
            import os
            import sys
            args = sys.argv[1:]
            with open(os.environ["API_CALLS"], "a") as output:
                output.write(json.dumps(args) + "\\n")
            if "--method" not in args:
                path = args[1]
                if "/git/commits/" in path:
                    print(os.environ["VERIFIED"])
                elif path.endswith("/git/ref/heads/main"):
                    print(os.environ["MAIN_SHA"])
                else:
                    sys.exit("Unexpected GET")
            elif args[args.index("--method") + 1] == "PATCH":
                # A sibling main commit arrives after the preceding GET.
                if os.environ.get("RACE") == "1":
                    sys.exit("Update is not a fast forward")
                if args[-2:] != ["-F", "force=false"]:
                    sys.exit("Expected a non-forced update")
            '''))
        gh.chmod(0o755)
        self.env = os.environ | {"PATH": f"{self.directory}:{os.environ['PATH']}",
                                 "API_CALLS": str(self.calls), "GITHUB_REPOSITORY": "liblaf/aur-mihomo-smart-bin",
                                 "GITHUB_SHA": "a" * 40, "COMMIT_SHA": "b" * 40,
                                 "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                                 "VERIFIED": "true", "MAIN_SHA": "a" * 40}

    def execute(self, name):
        return subprocess.run(("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step_script(name)),
                              cwd=self.directory, env=self.env, text=True, capture_output=True)

    def api_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_verified_commit_updates_stable_main_without_force(self):
        result = self.execute("Publish the verified commit to main")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.api_calls()
        self.assertEqual(len(calls), 3)
        self.assertIn("/git/commits/" + self.env["COMMIT_SHA"], calls[0][1])
        self.assertTrue(calls[1][1].endswith("/git/ref/heads/main"))
        self.assertIn("PATCH", calls[2])
        self.assertIn("sha=" + self.env["COMMIT_SHA"], calls[2])
        self.assertIn("force=false", calls[2])

    def test_unverified_commit_or_moved_main_blocks_update(self):
        for changes in ({"VERIFIED": "false"}, {"MAIN_SHA": "c" * 40}):
            with self.subTest(changes=changes):
                self.env.update(VERIFIED="true", MAIN_SHA="a" * 40)
                self.env.update(changes)
                self.calls.unlink(missing_ok=True)
                result = self.execute("Publish the verified commit to main")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("PATCH" in call for call in self.api_calls()))

    def test_api_race_rejection_fails_publication_and_cleanup_can_run(self):
        self.env["RACE"] = "1"
        result = self.execute("Publish the verified commit to main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a fast forward", result.stderr)
        cleanup = self.execute("Remove GitHub publication branch")
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        self.assertIn("DELETE", self.api_calls()[-1])
        self.assertTrue(self.api_calls()[-1][-1].endswith("/git/refs/heads/aur-automation/123-2"))

    def test_branch_creation_uses_validated_event_sha_and_attempt(self):
        result = self.execute("Prepare GitHub publication branch")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.api_calls()[0]
        self.assertIn("POST", call)
        self.assertIn("ref=refs/heads/aur-automation/123-2", call)
        self.assertIn("sha=" + self.env["GITHUB_SHA"], call)


if __name__ == "__main__":
    unittest.main()
