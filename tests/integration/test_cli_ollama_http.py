from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from main import run
from tests.helpers import prompt_targets


ROOT = Path(__file__).resolve().parents[2]


class _OllamaHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json_response(self, value: dict) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/api/tags":
            self._json_response({"models": [{"name": "test-hy-mt"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/api/generate":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(payload)
        rows = []
        for segment_id, _language, source in prompt_targets(payload["prompt"]):
            translations = {
                "Hello [[PH_0001]]": "안녕 [[PH_0001]]",
                "世界": "세계",
            }
            rows.append(f"{segment_id}\t{translations[source]}")
        self._json_response({"response": "\n".join(rows), "done": True})


class CliOllamaHttpIntegrationTests(unittest.TestCase):
    def test_main_txt_to_korean_txt_through_ollama_http_contract(self) -> None:
        _OllamaHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                input_path = temp / "novel.txt"
                input_path.write_text("Hello %PLAYER%\r\n\r\n世界", encoding="utf-8", newline="")

                config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                config["ollama"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
                config["ollama"]["model"] = "test-hy-mt"
                config_path = temp / "config.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )

                exit_code = run(
                    [
                        str(input_path),
                        "--config",
                        str(config_path),
                        "--profile",
                        str(ROOT / "profiles" / "novel.json"),
                    ]
                )
                self.assertEqual(exit_code, 0)
                with (temp / "novel.ko.txt").open(
                    "r", encoding="utf-8", newline=""
                ) as translated_file:
                    translated_text = translated_file.read()
                self.assertEqual(translated_text, "안녕 %PLAYER%\r\n\r\n세계")
                self.assertTrue((temp / "novel.seori.sqlite").is_file())
                report = json.loads((temp / "novel.qa.json").read_text(encoding="utf-8"))
                self.assertEqual(report["summary"]["valid"], 2)
                self.assertEqual(report["summary"]["failed"], 0)
                self.assertEqual(len(list((temp / "backup").glob("novel.*.txt"))), 1)
                self.assertEqual(len(_OllamaHandler.requests), 1)
                self.assertEqual(_OllamaHandler.requests[0]["model"], "test-hy-mt")
                self.assertEqual(
                    _OllamaHandler.requests[0]["options"],
                    {
                        "top_k": 20,
                        "top_p": 0.6,
                        "repeat_penalty": 1.05,
                        "temperature": 0.7,
                    },
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
