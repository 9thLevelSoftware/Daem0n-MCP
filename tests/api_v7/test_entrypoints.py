from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


class HttpEntrypointTests(unittest.TestCase):
    def test_http_launcher_builds_remote_v7_server_and_uses_shared_runner(self) -> None:
        import start_server
        from daem0nmcp.api.v7.launcher import ServerOptions

        workspace = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workspace, True)

        built: list[tuple[str, str | None]] = []
        launched: list[tuple[object, ServerOptions]] = []
        server = object()

        def create_server(
            transport: str,
            *,
            host: str | None = None,
        ) -> object:
            self.assertEqual(
                os.environ.get("DAEM0NMCP_PROJECT_ROOT"),
                str(workspace.resolve()),
            )
            built.append((transport, host))
            return server

        fake_server = types.ModuleType("daem0nmcp.server")
        fake_server.create_server = create_server
        arguments = [
            "start_server.py",
            "--host",
            "127.0.0.1",
            "--port",
            "9988",
            "--project",
            str(workspace),
        ]
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"daem0nmcp.server": fake_server}),
            patch.object(sys, "argv", arguments),
            patch.dict(os.environ, {}, clear=True),
            patch(
                "daem0nmcp.api.v7.launcher.run_server",
                side_effect=lambda built_server, options: launched.append(
                    (built_server, options)
                ),
            ),
            redirect_stdout(output),
        ):
            start_server.main()

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(built, [("streamable-http", "127.0.0.1")])
        self.assertEqual(
            launched,
            [(server, ServerOptions("streamable-http", "127.0.0.1", 9988))],
        )


if __name__ == "__main__":
    unittest.main()
