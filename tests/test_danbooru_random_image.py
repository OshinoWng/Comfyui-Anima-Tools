"""Offline tests for the Danbooru random image node.

Everything here runs without ComfyUI, Pillow, torch or the network: the module is
loaded with the same ``ast`` trick the other tests use, and the two things that
touch the outside world are seams that get replaced - ``http_get`` for every HTTP
request and ``load_image_tensor`` for the Pillow/torch decode.  What is left to
test is exactly what this node owns: which request it builds, which post it picks
out of the answer, how it formats the tags for Anima, what it caches, and how it
behaves when the site (or the poisoned DNS) misbehaves.
"""

import ast
import base64
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import tempfile
import time
import unittest
import urllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MODULE_NAME = "nodes_danbooru.py"

# The module is exec'd from its top-level statements, so its imports never run and
# every stdlib name it uses has to be handed over by hand.
STDLIB_NAMES = (
    "base64",
    "hashlib",
    "http",
    "json",
    "os",
    "random",
    "re",
    "socket",
    "ssl",
    "tempfile",
    "time",
    "urllib",
)


def load_danbooru_namespace():
    module_path = Path(__file__).resolve().parents[1] / MODULE_NAME
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))
    ]
    namespace = {"__file__": str(module_path), "__name__": "nodes_danbooru"}
    namespace.update(
        {
            "base64": base64,
            "hashlib": hashlib,
            "http": http,
            "json": json,
            "os": os,
            "random": random,
            "re": re,
            "socket": socket,
            "ssl": ssl,
            "tempfile": tempfile,
            "time": time,
            "urllib": urllib,
        }
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), str(module_path), "exec"),
        namespace,
    )
    return namespace


def sample_post(**overrides):
    """A post shaped like a real ``/posts.json`` entry (id 12361469, trimmed)."""

    post = {
        "id": 12361469,
        "md5": "00dec0ec3dcd33dfd49bdbdb627757b0",
        "file_ext": "jpg",
        "file_size": 162748,
        "rating": "g",
        "score": 12,
        "source": "https://example.invalid/art",
        "image_width": 914,
        "image_height": 1200,
        # Trimmed from the live response; some of these sit in ``meta`` on the real
        # site, and both spellings exist in the wild, so the blocklist has to cope.
        "tag_string_general": "1girl solo artist_name black_hair character_request",
        "tag_string_character": "irene_(arknights)",
        "tag_string_copyright": "arknights",
        "tag_string_artist": "sakuratani_hane",
        "tag_string_meta": "absurdres highres",
        "file_url": "https://cdn.donmai.us/original/f3/7e/payload.jpg",
        "large_file_url": "https://cdn.donmai.us/sample/f3/7e/sample-payload.jpg",
        "preview_file_url": "https://cdn.donmai.us/180x180/f3/7e/payload.jpg",
        "media_asset": {
            "variants": [
                {"type": "180x180", "url": "https://cdn.donmai.us/180x180/f3/7e/payload.jpg"},
                {"type": "360x360", "url": "https://cdn.donmai.us/360x360/f3/7e/payload.jpg"},
                {
                    "type": "720x720",
                    "url": "https://cdn.donmai.us/720x720/f3/7e/payload.webp",
                    "file_ext": "webp",
                },
                {"type": "sample", "url": "https://cdn.donmai.us/sample/f3/7e/sample-payload.jpg"},
                {"type": "original", "url": "https://cdn.donmai.us/original/f3/7e/payload.jpg"},
            ]
        },
    }
    post.update(overrides)
    return post


class FakeHttp:
    """Stands in for ``http_get``: answers by URL, remembers what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, timeout=None, headers=None):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError("unexpected request: %s" % url)
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(url, headers)
        if isinstance(answer, (dict, list)):
            return json.dumps(answer).encode("utf-8")
        return answer


class DanbooruNamespaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_danbooru_namespace()

    def setUp(self):
        self.node_class = self.ns["AnimaDanbooruRandomImage"]
        self.node = self.node_class()

    # --- tag formatting ----------------------------------------------------

    def test_tag_groups_are_normalised_and_joined_with_commas(self):
        groups = self.ns["format_tag_groups"](sample_post())

        self.assertEqual(groups["character"], "irene (arknights)")
        self.assertEqual(groups["copyright"], "arknights")
        self.assertEqual(groups["general"], "1girl, solo, black hair")

    def test_artist_tags_get_the_at_prefix_anima_uses(self):
        groups = self.ns["format_tag_groups"](sample_post(), tag_types=("artist",))

        self.assertEqual(groups["artist"], "@sakuratani hane")

    def test_the_default_blocklist_drops_danbooru_noise(self):
        post = sample_post(tag_string_general="1girl artist_name commentary_request bad_id smirk")

        groups = self.ns["format_tag_groups"](post)

        self.assertEqual(groups["general"], "1girl, smirk")

    def test_the_blocklist_can_be_turned_off_and_extended(self):
        post = sample_post(tag_string_general="1girl artist_name smirk")

        kept = self.ns["format_tag_groups"](post, block_tags=(), use_default_blocklist=False)
        extended = self.ns["format_tag_groups"](post, block_tags=("smirk",))

        self.assertIn("artist name", kept["general"])
        self.assertEqual(extended["general"], "1girl")

    def test_meta_is_empty_unless_it_is_asked_for(self):
        default_groups = self.ns["format_tag_groups"](sample_post())
        meta_groups = self.ns["format_tag_groups"](sample_post(), tag_types=("meta",))

        self.assertEqual(default_groups["meta"], "")
        self.assertNotIn("highres", default_groups["prompt"])
        self.assertEqual(meta_groups["meta"], "absurdres, highres")

    def test_the_prompt_joins_the_groups_in_anima_order(self):
        groups = self.ns["format_tag_groups"](sample_post())

        self.assertEqual(
            groups["prompt"],
            "irene (arknights), arknights, 1girl, solo, black hair",
        )

    def test_an_empty_post_formats_to_empty_strings(self):
        groups = self.ns["format_tag_groups"]({})

        self.assertEqual(groups["prompt"], "")
        self.assertEqual(groups["character"], "")

    # --- building the request ---------------------------------------------

    def test_a_random_seed_asks_the_site_for_a_random_post(self):
        request = self.ns["build_request"]("rating:general", -1)

        self.assertIn("random:1", request["tags"])
        self.assertEqual(request["limit"], 1)
        self.assertIsNone(request["page"])
        self.assertEqual(request["mode"], "random")

    def test_a_fixed_seed_maps_to_a_stable_page_and_index(self):
        first = self.ns["build_request"]("rating:general", 20261012)
        repeated = self.ns["build_request"]("rating:general", 20261012)
        other = self.ns["build_request"]("rating:general", 20261013)

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other)
        self.assertEqual(first["mode"], "page")
        self.assertNotIn("random:1", first["tags"], "the page path must not spend a tag slot")
        self.assertTrue(1 <= first["page"] <= self.ns["MAX_PAGE"])
        self.assertTrue(0 <= first["index"] < first["limit"])

    def test_query_tags_are_normalised_and_defaulted(self):
        self.assertEqual(self.ns["build_request"]("1Girl, Solo", -1)["tags"], "1girl solo random:1")
        self.assertEqual(
            self.ns["build_request"]("   ", -1)["tags"], "rating:general random:1"
        )

    def test_query_tags_keep_underscores_and_drop_duplicates(self):
        """Search talks about ``black_hair`` - the space form is for prompts only."""
        request = self.ns["build_request"]("black_hair, black_hair, 1Girl", 7)

        self.assertEqual(request["tags"], "black_hair 1girl")
        self.assertEqual(
            self.ns["normalize_query_tags"]("black hair"),
            ["black", "hair"],
            "the site reads a space as a separator, so it must stay two tags",
        )

    # --- choosing a post ---------------------------------------------------

    def test_unusable_posts_are_skipped(self):
        posts = [
            sample_post(id=1, is_deleted=True),
            sample_post(id=2, file_ext="mp4", file_url="https://cdn.donmai.us/original/a.mp4"),
            sample_post(id=3, tag_string_general="1girl animated"),
            sample_post(id=4),
        ]

        chosen = self.ns["pick_post"](posts, 0)

        self.assertEqual(chosen["id"], 4)

    def test_pick_post_returns_none_when_nothing_is_usable(self):
        posts = [sample_post(file_ext="mp4"), sample_post(is_banned=True)]

        self.assertIsNone(self.ns["pick_post"](posts, 0))

    def test_the_requested_size_picks_the_matching_variant(self):
        post = sample_post()

        self.assertEqual(
            self.ns["choose_image_url"](post, "sample"),
            "https://cdn.donmai.us/sample/f3/7e/sample-payload.jpg",
        )
        self.assertEqual(
            self.ns["choose_image_url"](post, "720x720"),
            "https://cdn.donmai.us/720x720/f3/7e/payload.webp",
        )

    def test_the_url_falls_back_when_there_is_no_variant_list(self):
        post = sample_post()
        post.pop("media_asset")

        self.assertEqual(self.ns["choose_image_url"](post, "sample"), post["large_file_url"])
        self.assertEqual(self.ns["choose_image_url"](post, "original"), post["file_url"])
        self.assertIsNone(self.ns["choose_image_url"]({}, "sample"))

    # --- the node itself ---------------------------------------------------

    def test_run_downloads_the_image_and_reuses_it_from_the_cache(self):
        post = sample_post()
        # search, image, then the search of the second run (the image comes from
        # the cache, so it is never downloaded twice).
        http = FakeHttp([[post], b"IMAGEBYTES", [post]])
        self.ns["http_get"] = http
        self.ns["load_image_tensor"] = lambda data: ("tensor", data)
        with tempfile.TemporaryDirectory() as cache_dir:
            first = self.node.fetch_random_post(
                query_tags="rating:general", seed=20261012, cache_dir=cache_dir
            )
            requests_for_the_first_run = len(http.urls)
            second = self.node.fetch_random_post(
                query_tags="rating:general", seed=20261012, cache_dir=cache_dir
            )

            cached_file = os.path.join(cache_dir, "%s.jpg" % post["md5"])
            self.assertTrue(os.path.isfile(cached_file))

        self.assertEqual(first[0], ("tensor", b"IMAGEBYTES"))
        self.assertEqual(first[1], "")  # artist off by default
        self.assertEqual(first[2], "irene (arknights)")
        self.assertEqual(first[5], "irene (arknights), arknights, 1girl, solo, black hair")
        self.assertEqual(json.loads(first[6])["id"], post["id"])
        self.assertFalse(json.loads(first[6])["cached"])
        self.assertEqual(requests_for_the_first_run, 2, "one API request plus one image request")
        self.assertTrue(json.loads(second[6])["cached"])
        self.assertEqual(len(http.urls), 3, "the second run refetches the post, not the image")

    def test_run_retries_when_the_random_post_is_a_video(self):
        http = FakeHttp(
            [
                [sample_post(file_ext="mp4")],
                [sample_post(id=999)],
                b"IMAGEBYTES",
            ]
        )
        self.ns["http_get"] = http
        self.ns["load_image_tensor"] = lambda data: ("tensor", data)
        with tempfile.TemporaryDirectory() as cache_dir:
            result = self.node.fetch_random_post(query_tags="rating:general", cache_dir=cache_dir)

        self.assertEqual(json.loads(result[6])["attempts"], 2)
        self.assertEqual(json.loads(result[6])["id"], 999)

    def test_run_gives_up_with_a_readable_error(self):
        # One random draw per attempt, so the site is asked twice for two tries.
        self.ns["http_get"] = FakeHttp(
            [[sample_post(file_ext="mp4")], [sample_post(file_ext="mp4")]]
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(RuntimeError) as caught:
                self.node.fetch_random_post(
                    query_tags="rating:general", max_attempts=2, cache_dir=cache_dir
                )

        self.assertIn("2 attempt", str(caught.exception))

    def test_run_surfaces_the_sites_own_error_message(self):
        self.ns["http_get"] = FakeHttp(
            [
                {
                    "success": False,
                    "error": "PostQuery::TagLimitError",
                    "message": "You cannot search for more than 2 tags at a time.",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(RuntimeError) as caught:
                self.node.fetch_random_post(
                    query_tags="1girl solo cup", cache_dir=cache_dir
                )

        message = str(caught.exception)
        self.assertIn("more than 2 tags", message)
        self.assertIn("rating:", message, "the hint has to explain that rating: is free")

    def test_run_reports_an_empty_result_set(self):
        self.ns["http_get"] = FakeHttp([[]])
        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaises(RuntimeError) as caught:
                self.node.fetch_random_post(query_tags="unicorn nobody", cache_dir=cache_dir)

        self.assertIn("unicorn nobody", str(caught.exception))

    def test_credentials_fall_back_to_the_environment(self):
        os.environ["DANBOORU_LOGIN"] = "someone"
        os.environ["DANBOORU_API_KEY"] = "secretkey"
        self.addCleanup(os.environ.pop, "DANBOORU_LOGIN", None)
        self.addCleanup(os.environ.pop, "DANBOORU_API_KEY", None)
        seen = {}

        def capture(url, timeout=None, headers=None):
            seen.update(headers or {})
            return json.dumps([sample_post()]).encode("utf-8")

        self.ns["http_get"] = capture
        self.ns["load_image_tensor"] = lambda data: ("tensor", data)
        with tempfile.TemporaryDirectory() as cache_dir:
            self.node.fetch_random_post(query_tags="rating:general", cache_dir=cache_dir)

        expected = base64.b64encode(b"someone:secretkey").decode("ascii")
        self.assertEqual(seen.get("Authorization"), "Basic %s" % expected)

    def test_the_api_key_never_reaches_the_url_or_the_output(self):
        calls = []

        def capture(url, timeout=None, headers=None):
            calls.append((url, dict(headers or {})))
            if "posts.json" in url:
                return json.dumps([sample_post()]).encode("utf-8")
            return b"IMAGEBYTES"

        self.ns["http_get"] = capture
        self.ns["load_image_tensor"] = lambda data: ("tensor", data)
        with tempfile.TemporaryDirectory() as cache_dir:
            result = self.node.fetch_random_post(
                query_tags="rating:general",
                login="someone",
                api_key="secretkey",
                cache_dir=cache_dir,
            )

        for url, headers in calls:
            self.assertNotIn("secretkey", url)
            if "cdn.donmai.us" in url:
                self.assertNotIn(
                    "Authorization",
                    headers,
                    "the API key has no business going to the CDN",
                )
        for value in result:
            self.assertNotIn("secretkey", str(value))

    def test_run_needs_no_credentials_at_all(self):
        self.ns["http_get"] = FakeHttp([[sample_post()], b"IMAGEBYTES"])
        self.ns["load_image_tensor"] = lambda data: ("tensor", data)
        with tempfile.TemporaryDirectory() as cache_dir:
            result = self.node.fetch_random_post(query_tags="rating:general", cache_dir=cache_dir)

        self.assertEqual(result[0], ("tensor", b"IMAGEBYTES"))

    # --- the network layer -------------------------------------------------

    def test_a_poisoned_dns_falls_back_to_the_pinned_ips(self):
        asked = []

        def fail(url, timeout=None, headers=None):
            asked.append(("plain", url))
            raise urllib.error.URLError("getaddrinfo failed")

        def pinned(url, timeout=None, headers=None):
            asked.append(("pinned", url))
            return b"PINNED"

        self.ns["_plain_get"] = fail
        self.ns["_pinned_get"] = pinned

        self.assertEqual(self.ns["http_get"]("https://danbooru.donmai.us/posts.json"), b"PINNED")
        self.assertEqual([kind for kind, _ in asked], ["plain", "pinned"])

    def test_a_real_http_error_is_not_retried_through_the_pins(self):
        self.ns["_plain_get"] = lambda url, timeout=None, headers=None: (_ for _ in ()).throw(
            self.ns["DanbooruError"]("HTTP 403: forbidden")
        )
        self.ns["_pinned_get"] = lambda url, timeout=None, headers=None: b"SHOULD NOT RUN"

        with self.assertRaises(self.ns["DanbooruError"]) as caught:
            self.ns["http_get"]("https://danbooru.donmai.us/posts.json")

        self.assertIn("403", str(caught.exception))

    def test_every_host_that_matters_has_a_pinned_ip(self):
        pins = self.ns["HOST_PINS"]

        self.assertIn("danbooru.donmai.us", pins)
        self.assertIn("cdn.donmai.us", pins)
        for host, ips in pins.items():
            with self.subTest(host=host):
                self.assertTrue(ips, "no candidate IP for %s" % host)
                for ip in ips:
                    self.assertRegex(ip, r"^\d{1,3}(\.\d{1,3}){3}$")

    def test_the_user_agent_names_the_project_as_the_site_asks(self):
        headers = self.ns["build_headers"]()

        self.assertIn("Anima-Tools", headers["User-Agent"])
        self.assertIn("User-Agent", headers)

    def test_a_half_filled_credential_pair_is_ignored_with_a_warning(self):
        headers = self.ns["build_headers"]("someone", "")

        self.assertNotIn("Authorization", headers)

    def test_the_pinned_connection_keeps_the_real_hostname_for_tls(self):
        """Connecting to an IP must still verify the certificate of the hostname."""
        connection_class = self.ns["_SniHTTPSConnection"]
        connection = connection_class("danbooru.donmai.us", "104.26.10.39", timeout=5)

        self.assertEqual(connection.host, "danbooru.donmai.us")
        self.assertEqual(connection.port, 443)
        self.assertEqual(connection._ip, "104.26.10.39")

    # --- caching key -------------------------------------------------------

    def test_a_random_seed_asks_for_a_new_image_every_queue(self):
        first = self.node_class.IS_CHANGED(seed=-1, query_tags="rating:general")
        time.sleep(0.002)
        second = self.node_class.IS_CHANGED(seed=-1, query_tags="rating:general")

        self.assertNotEqual(first, second)

    def test_a_fixed_seed_is_stable_and_sensitive_to_the_query(self):
        first = self.node_class.IS_CHANGED(seed=7, query_tags="rating:general")
        repeated = self.node_class.IS_CHANGED(seed=7, query_tags="rating:general")
        other_query = self.node_class.IS_CHANGED(seed=7, query_tags="rating:general solo")
        other_seed = self.node_class.IS_CHANGED(seed=8, query_tags="rating:general")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_query)
        self.assertNotEqual(first, other_seed)

    def test_a_linked_seed_leaves_the_decision_to_comfyui(self):
        first = self.node_class.IS_CHANGED(seed=None, query_tags="rating:general")
        repeated = self.node_class.IS_CHANGED(seed=None, query_tags="rating:general solo")
        as_list = self.node_class.IS_CHANGED(seed=["610", 0], query_tags="rating:general")

        self.assertEqual(first, repeated)
        self.assertEqual(first, as_list)
        self.assertNotEqual(first, self.node_class.IS_CHANGED(seed=-1, query_tags="rating:general"))

    def test_is_changed_does_not_hash_the_api_key(self):
        """Rotating a key must not look like a reason to redraw."""
        first = self.node_class.IS_CHANGED(seed=7, query_tags="rating:general", api_key="a")
        second = self.node_class.IS_CHANGED(seed=7, query_tags="rating:general", api_key="b")

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
