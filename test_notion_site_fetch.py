import unittest
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from notion_site_fetch import (
    render_block_lines,
    render_equation,
    render_rich_text,
    resolve_page_and_space,
)


class ContentTests(unittest.TestCase):
    def test_inline_equation_replaces_placeholder_and_keeps_neighboring_text(self):
        runs = [["State "], ["⁍", [["e", r"s_i \\rightarrow a_i"]]], [" scores."]]
        self.assertEqual(render_rich_text(runs), r"State $s_i \rightarrow a_i$ scores.")

    def test_equation_decodes_only_one_layer(self):
        source = r"\\begin{matrix}a & b \\\\ c & d\\end{matrix}"
        self.assertEqual(render_equation(source), r"\begin{matrix}a & b \\ c & d\end{matrix}")
        self.assertEqual(render_equation(r"\\nu + \\nabla"), r"\nu + \nabla")

    def test_single_escaped_command_is_not_a_newline(self):
        self.assertEqual(render_equation(r"\nu + \nabla"), r"\nu + \nabla")

    def test_display_equation_preserves_multiline_tex(self):
        blocks = {"eq": {"type": "equation", "properties": {
            "title": [[r"\\mathcal{L}\n=\\frac{a}{b}"]]
        }}}
        self.assertEqual(render_block_lines("eq", blocks, 1, 0), [
            "    $$", r"    \mathcal{L}", r"    =\frac{a}{b}", "    $$", ""
        ])

    def test_attachment_image_has_public_url_and_caption(self):
        source = "attachment:1234:plot (1).png"
        blocks = {"image-id": {"type": "image", "properties": {
            "source": [[source]], "caption": [["Measured accuracy"]]
        }}}
        line = render_block_lines("image-id", blocks, 0, 0)[0]
        self.assertTrue(line.startswith("![Measured accuracy](https://www.notion.so/image/"))
        parsed = urlparse(line.split("](", 1)[1][:-1])
        self.assertEqual(unquote(parsed.path.removeprefix("/image/")), source)
        self.assertEqual(parse_qs(parsed.query), {
            "table": ["block"], "id": ["image-id"], "cache": ["v2"]
        })

    def test_external_image_is_unchanged(self):
        blocks = {"image": {"type": "image", "properties": {
            "source": [["https://example.org/image.png?a=1&b=2"]]
        }}}
        self.assertEqual(render_block_lines("image", blocks, 0, 0)[0],
                         "![](https://example.org/image.png?a=1&b=2)")

    def test_small_heading_inside_toggle_is_not_flattened(self):
        blocks = {"toggle": {"type": "toggle", "properties": {"title": [["Details"]]},
                             "content": ["heading"]},
                  "heading": {"type": "header_4", "properties": {"title": [["Results"]]}}}
        self.assertIn("    ##### Results", render_block_lines("toggle", blocks, 0, 0))

    def test_site_root_uses_bootstrap_without_space_lookup(self):
        pid = "12345678-1234-1234-1234-123456789abc"
        def handle(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, text=(
                '<script>__notion_html_async.push("requiredRedirectMetadata",'
                '{"pageId":"' + pid + '","requiresRedirect":false})</script>'
            ))
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(resolve_page_and_space(
                "https://example.notion.site", client, "https://example.notion.site/api/v3"
            ), (pid, None))

    def test_direct_page_id_does_not_fetch_html(self):
        def handle(request):
            self.fail("A direct page id should not require a lookup")
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(resolve_page_and_space(
                "https://example.notion.site/12345678123412341234123456789abc",
                client, "https://example.notion.site/api/v3"
            ), ("12345678-1234-1234-1234-123456789abc", None))

    def test_site_without_bootstrap_retains_space_lookup(self):
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, text="<html></html>")
            if request.url.path.endswith("getPublicPageData"):
                return httpx.Response(200, json={"spaceId": "space"})
            return httpx.Response(200, json={"results": [{"publicHomePage": "home"}]})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(resolve_page_and_space(
                "https://example.notion.site", client, "https://example.notion.site/api/v3"
            ), ("home", "space"))


if __name__ == "__main__":
    unittest.main()
