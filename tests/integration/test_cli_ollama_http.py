from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from main import run
from tests.helpers import native_prompt_source


ROOT = Path(__file__).resolve().parents[2]


class _OllamaHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    source_counts: dict[str, int] = {}
    fail_once_sources: set[str] = set()

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
        source = native_prompt_source(payload["prompt"])
        type(self).source_counts[source] = type(self).source_counts.get(source, 0) + 1
        translations = {
            "Hello [[PH_0001]]": "안녕 [[PH_0001]]",
            "世界": "세계",
            "壊れた": "복구 번역",
        }
        if (
            source in type(self).fail_once_sources
            and type(self).source_counts[source] == 1
        ):
            response = "오류的"
        else:
            response = translations[source]
        self._json_response({"response": response, "done": True})


class CliOllamaHttpIntegrationTests(unittest.TestCase):
    def test_main_txt_to_korean_txt_for_all_modes_and_resume(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for mode in ("novel", "game", "document"):
                with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                    _OllamaHandler.requests = []
                    _OllamaHandler.source_counts = {}
                    _OllamaHandler.fail_once_sources = set()
                    temp = Path(directory)
                    input_path = temp / "input.txt"
                    input_path.write_text(
                        "Hello %PLAYER%\r\n\r\n世界", encoding="utf-8", newline=""
                    )

                    config = json.loads(
                        (ROOT / "config.json").read_text(encoding="utf-8")
                    )
                    config["ollama"]["base_url"] = (
                        f"http://127.0.0.1:{server.server_port}"
                    )
                    config["ollama"]["model"] = "test-hy-mt"
                    config_path = temp / "config.json"
                    config_path.write_text(
                        json.dumps(config, ensure_ascii=False), encoding="utf-8"
                    )

                    arguments = [
                        str(input_path),
                        "--config",
                        str(config_path),
                        "--mode",
                        mode,
                    ]
                    self.assertEqual(run(arguments), 0)
                    with (temp / "input.ko.txt").open(
                        "r", encoding="utf-8", newline=""
                    ) as translated_file:
                        translated_text = translated_file.read()
                    self.assertEqual(translated_text, "안녕 %PLAYER%\r\n\r\n세계")
                    self.assertTrue((temp / "input.seori.sqlite").is_file())
                    report = json.loads(
                        (temp / "input.qa.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(report["mode"], mode)
                    self.assertEqual(report["profile"], mode)
                    self.assertEqual(report["summary"]["valid"], 2)
                    self.assertEqual(report["summary"]["failed"], 0)
                    self.assertEqual(
                        len(list((temp / "backup").glob("input.*.txt"))), 1
                    )
                    self.assertEqual(len(_OllamaHandler.requests), 2)
                    self.assertTrue(
                        all(
                            "SEG_" not in request["prompt"]
                            and "ADULT_" not in request["prompt"]
                            for request in _OllamaHandler.requests
                        )
                    )
                    self.assertEqual(
                        _OllamaHandler.requests[0]["model"], "test-hy-mt"
                    )
                    self.assertEqual(
                        _OllamaHandler.requests[0]["options"],
                        {
                            "top_k": 20,
                            "top_p": 0.6,
                            "repeat_penalty": 1.05,
                            "temperature": 0.7,
                        },
                    )

                    # Ordinary resume must reuse VALID rows and the existing backup.
                    self.assertEqual(run(arguments), 0)
                    self.assertEqual(len(_OllamaHandler.requests), 2)
                    self.assertEqual(
                        len(list((temp / "backup").glob("input.*.txt"))), 1
                    )
                    resumed_report = json.loads(
                        (temp / "input.qa.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(resumed_report["summary"]["resumed"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_segment_repairs_without_retranslating_valid_sibling(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                _OllamaHandler.requests = []
                _OllamaHandler.source_counts = {}
                _OllamaHandler.fail_once_sources = {"壊れた"}
                temp = Path(directory)
                input_path = temp / "input.txt"
                input_path.write_text("世界\n\n壊れた", encoding="utf-8")

                config = json.loads(
                    (ROOT / "config.json").read_text(encoding="utf-8")
                )
                config["ollama"]["base_url"] = (
                    f"http://127.0.0.1:{server.server_port}"
                )
                config["ollama"]["model"] = "test-hy-mt"
                config_path = temp / "config.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )

                self.assertEqual(
                    run(
                        [
                            str(input_path),
                            "--config",
                            str(config_path),
                            "--mode",
                            "novel",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    (temp / "input.ko.txt").read_text(encoding="utf-8"),
                    "세계\n\n복구 번역",
                )
                self.assertEqual(_OllamaHandler.source_counts["世界"], 1)
                self.assertEqual(_OllamaHandler.source_counts["壊れた"], 2)
                self.assertEqual(len(_OllamaHandler.requests), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
