from __future__ import annotations

import httpx
import pytest

from crawler.acl import canonical_paper_url, paper_id_from_url, validate_volume_url
from crawler.openreview import (
    OpenReviewChallengeError,
    OpenReviewClient,
    OpenReviewError,
    collection_from_url,
    discover_from_html,
    parse_forum_html,
    parse_note,
)


def test_openreview_collection_and_forum_urls_are_canonical() -> None:
    source = "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight"
    collection = collection_from_url(source)
    assert collection.venue == "ICML 2026 spotlight"
    assert collection.decision == "Accept (spotlight)"
    assert collection.invitation == "ICML.cc/2026/Conference/-/Submission"
    assert validate_volume_url(source) == collection.url
    forum = "https://www.openreview.net/forum?id=aIH1jyU37z&referrer=x"
    assert canonical_paper_url(forum) == "https://openreview.net/forum?id=aIH1jyU37z"
    assert paper_id_from_url(forum) == "openreview_aIH1jyU37z"


def test_openreview_discovery_is_scoped_to_selected_tab() -> None:
    html = """
    <div id="accept-spotlight"><a href="/forum?id=spot001">Spotlight paper</a></div>
    <div id="accept-regular"><a href="/forum?id=regular1">Regular paper</a></div>
    <a href="/forum?id=outside1">Unrelated</a>
    """
    refs = discover_from_html(
        html,
        "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight",
    )
    assert [ref.forum_id for ref in refs] == ["spot001"]


def test_openreview_discovery_accepts_aria_linked_tabpanel() -> None:
    html = """
    <button id="tab-accept-spotlight" aria-selected="true">Spotlight</button>
    <section role="tabpanel" aria-labelledby="tab-accept-spotlight">
      <a href="/forum?id=spot002">Spotlight paper</a>
    </section>
    <section role="tabpanel" aria-labelledby="tab-accept-regular">
      <a href="/forum?id=regular2">Regular paper</a>
    </section>
    """
    refs = discover_from_html(
        html,
        "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight",
    )
    assert [ref.forum_id for ref in refs] == ["spot002"]


def test_openreview_note_maps_requested_metadata() -> None:
    note = {
        "id": "aIH1jyU37z",
        "pdate": 1777593600000,
        "mdate": 1786214400000,
        "content": {
            "title": {"value": "Foundations of Equivariant Deep Learning"},
            "authors": {"value": ["Yoshihiro Maruyama"]},
            "authorids": {"value": ["~Yoshihiro_Maruyama2"]},
            "abstract": {"value": "An abstract."},
            "TL;DR": {"value": "A short summary."},
            "Lay Summary": {"value": "A lay summary."},
            "Primary Area": {"value": "Theory->Deep Learning"},
            "keywords": {"value": ["Equivariance", "GNN"]},
            "pdf": {"value": "/attachment?id=aIH1jyU37z&name=pdf"},
            "originally_submitted_PDF": {
                "value": "/attachment?id=aIH1jyU37z&name=originally_submitted_PDF"
            },
            "venue": {"value": "ICML 2026 spotlight"},
            "decision": {"value": "Accept (spotlight)"},
        },
        "number": 34584,
    }
    paper = parse_note(note)
    assert paper.title == "Foundations of Equivariant Deep Learning"
    assert paper.abstract_en == "An abstract."
    assert paper.author_profiles[0]["url"].endswith("~Yoshihiro_Maruyama2")
    assert paper.pdf_url.startswith("https://openreview.net/attachment?id=")
    assert paper.original_pdf_url.endswith("name=originally_submitted_PDF")
    assert paper.decision == "Accept (spotlight)"
    assert paper.submission_number == "34584"
    assert paper.title_translation_failed
    assert paper.translation_failed


def test_openreview_client_paginates_and_deduplicates() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        calls.append(query)
        offset = int(query.get("offset", "0"))
        notes = [] if offset >= 2 else [
            {"id": "paper01", "content": {"title": "One", "authors": ["A"]}},
            {"id": "paper02", "content": {"title": "Two", "authors": ["B"]}},
        ]
        # A repeated page is intentionally returned to make sure the client
        # still terminates when a provider ignores offset.
        if offset == 2:
            notes = [{"id": "paper02", "content": {"title": "Two"}}]
        return httpx.Response(200, json={"notes": notes, "count": 2}, request=request)

    client = OpenReviewClient(
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    try:
        collection = collection_from_url(
            "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight"
        )
        refs = client.discover(collection)
    finally:
        client.close()
    assert [ref.forum_id for ref in refs] == ["paper01", "paper02"]
    assert calls[0]["content.venue"] == "ICML 2026 spotlight"
    assert calls[0]["invitation"] == "ICML.cc/2026/Conference/-/Submission"


def test_openreview_client_follows_server_page_size_until_count() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        calls.append(offset)
        notes = [
            {"id": f"paper{index:03d}", "content": {"title": f"Paper {index}"}}
            for index in range(offset, min(offset + 25, 53))
        ]
        return httpx.Response(200, json={"notes": notes, "count": 53}, request=request)

    client = OpenReviewClient(
        transport=httpx.MockTransport(handler), sleeper=lambda _seconds: None
    )
    try:
        refs = client.discover(
            collection_from_url(
                "https://openreview.net/group?id=ICML.cc/2026/Conference"
                "#tab-accept-spotlight"
            )
        )
    finally:
        client.close()

    assert len(refs) == 53
    assert calls == [0, 25, 50]


def test_openreview_client_rejects_repeated_partial_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        notes = [
            {"id": "paper001", "content": {"title": "One"}},
            {"id": "paper002", "content": {"title": "Two"}},
        ]
        return httpx.Response(200, json={"notes": notes, "count": 4}, request=request)

    client = OpenReviewClient(
        transport=httpx.MockTransport(handler), sleeper=lambda _seconds: None
    )
    try:
        with pytest.raises(OpenReviewError, match="分页未前进"):
            client.discover(
                collection_from_url(
                    "https://openreview.net/group?id=ICML.cc/2026/Conference"
                    "#tab-accept-spotlight"
                )
            )
    finally:
        client.close()


def test_openreview_forum_html_keeps_official_decision_comment() -> None:
    html = """
    <html><head><meta name="citation_author" content="Yoshihiro Maruyama"></head>
      <h2 class="citation_title">Foundations of Equivariant Deep Learning</h2>
      <div>Published: 01 May 2026, Last Modified: 08 Aug 2026ICML 2026 spotlight</div>
      <div><strong class="note-content-field">Abstract:</strong>
        <div class="note-content-value">Symmetry is everywhere.</div></div>
      <div class="note" data-id="decision1">
        <div class="heading"><h4>Paper Decision</h4></div>
        <strong class="note-content-field">Decision:</strong>
        <span class="note-content-value">Accept (spotlight)</span>
        <strong class="note-content-field">Comment:</strong>
        <div class="note-content-value">Reviewers were very positive.</div>
      </div>
      <div class="note"><div class="heading"><h4>Official Review</h4></div>
        <a href="/profile?id=~Reviewer1">Reviewer</a></div>
    </html>
    """
    paper = parse_forum_html(
        html,
        "https://openreview.net/forum?id=aIH1jyU37z",
        collection=collection_from_url(
            "https://openreview.net/group?id=ICML.cc/2026/Conference"
            "#tab-accept-spotlight"
        ),
    )

    assert paper.decision == "Accept (spotlight)"
    assert paper.decision_comment == "Reviewers were very positive."
    assert paper.authors == ["Yoshihiro Maruyama"]
    assert "Reviewer" not in paper.authors


def test_openreview_current_visible_fields_are_parsed() -> None:
    html = """
    <html><body><main>
      <h2>Foundations of Equivariant Deep Learning</h2>
      <h3><a href="/profile?id=~Yoshihiro_Maruyama2">Yoshihiro Maruyama</a></h3>
      <div>Published: 01 May 2026, Last Modified: 08 Aug 2026 ICML 2026 spotlight</div>
      <strong>TL;DR:</strong>
      Geometric deep learning can be extended beyond group symmetries
      <strong>Abstract:</strong>
      <p>Symmetry is everywhere in nature and society.</p>
      <strong>Lay Summary:</strong>
      <p>A unified foundation for structured models.</p>
      <strong>Primary Area:</strong>
      Theory-&gt;Deep Learning
      <strong>Keywords:</strong>
      Geometric Deep Learning, Topological Deep Learning
      <strong>Originally Submitted PDF:</strong>
      <a href="/attachment?id=aIH1jyU37z&amp;name=originally_submitted_PDF">pdf</a>
      <strong>Submission Number:</strong>
      34584
      <div class="note" data-id="decision-current">
        <h4>Paper Decision</h4>
        <strong>Decision:</strong>
        Accept (spotlight)
        <strong>Comment:</strong>
        <p>Reviewers were very positive.</p>
      </div>
    </main></body></html>
    """

    paper = parse_forum_html(
        html,
        "https://openreview.net/forum?id=aIH1jyU37z",
        collection=collection_from_url(
            "https://openreview.net/group?id=ICML.cc/2026/Conference"
            "#tab-accept-spotlight"
        ),
    )

    assert paper.authors == ["Yoshihiro Maruyama"]
    assert paper.tldr == "Geometric deep learning can be extended beyond group symmetries"
    assert paper.abstract_en == "Symmetry is everywhere in nature and society."
    assert paper.lay_summary == "A unified foundation for structured models."
    assert paper.primary_area == "Theory->Deep Learning"
    assert paper.keywords == ["Geometric Deep Learning", "Topological Deep Learning"]
    assert paper.submission_number == "34584"
    assert paper.decision == "Accept (spotlight)"
    assert paper.decision_comment == "Reviewers were very positive."
    assert paper.published_at == "01 May 2026"
    assert paper.modified_at == "08 Aug 2026"


def test_openreview_challenge_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"name": "ChallengeRequiredError", "message": "challenge"},
            request=request,
        )

    client = OpenReviewClient(
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    try:
        collection = collection_from_url(
            "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight"
        )
        with pytest.raises(OpenReviewChallengeError, match="浏览器验证"):
            client.discover(collection)
    finally:
        client.close()


def test_openreview_non_json_403_is_also_a_batch_challenge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Verify you are human", request=request)

    client = OpenReviewClient(
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    try:
        collection = collection_from_url(
            "https://openreview.net/group?id=ICML.cc/2026/Conference#tab-accept-spotlight"
        )
        with pytest.raises(OpenReviewChallengeError, match="HTTP 403"):
            client.discover(collection)
    finally:
        client.close()
