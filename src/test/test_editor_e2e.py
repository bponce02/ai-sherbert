"""End-to-end test for the Konva editor: draw a pen stroke in a real
browser and verify it persists through the API and re-renders on reload.

Requires Playwright chromium (`playwright install chromium`) and network
access for the pdf.js/Konva CDNs; skips cleanly when unavailable.
"""
import os

import pymupdf
import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile

from sherbert_pdf.models import PDFAnnotation, PDFDocument

# Playwright's sync API drives the browser from the test thread while
# live_server handles requests in another; Django's async-unsafe guard
# would otherwise refuse the ORM calls.
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

pytestmark = pytest.mark.skipif(not HAVE_PLAYWRIGHT, reason='playwright not installed')


@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f'chromium not available: {exc}')
        yield browser
        browser.close()


def make_pdf_bytes():
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.django_db(transaction=True)
def test_draw_pen_stroke_persists_and_rerenders(browser, client, live_server, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('owner', password='pw')
    pdf_doc = PDFDocument(title='E2E Doc', user=owner)
    pdf_doc.file.save('e2e.pdf', ContentFile(make_pdf_bytes()), save=True)

    client.force_login(owner)
    session_cookie = client.cookies[settings.SESSION_COOKIE_NAME]

    context = browser.new_context()
    context.add_cookies([{
        'name': settings.SESSION_COOKIE_NAME,
        'value': session_cookie.value,
        'url': live_server.url,
    }])
    page = context.new_page()

    page.goto(f'{live_server.url}/pdf/editor/{pdf_doc.pk}/')
    page.wait_for_function(
        'window.__sherbertEditor && (window.__sherbertEditor.ready || window.__sherbertEditor.error)',
        timeout=30000,
    )
    error = page.evaluate('window.__sherbertEditor.error || null')
    assert error is None, f'editor failed to initialize: {error}'

    page.click('[data-tool="pen"]')

    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None

    start_x, start_y = box['x'] + box['width'] * 0.3, box['y'] + box['height'] * 0.3
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'POST' and r.status == 201,
        timeout=15000,
    ):
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        for i in range(1, 6):
            page.mouse.move(start_x + i * 20, start_y + i * 10)
        page.mouse.up()

    annotation = PDFAnnotation.objects.get()
    assert annotation.annotation_type == 'pen'
    assert annotation.pdf_document_id == pdf_doc.pk
    assert annotation.pen_data.vertices and annotation.pen_data.vertices[0]

    # Reload: the stroke must come back from the API as a Konva node.
    page.reload()
    page.wait_for_function(
        'window.__sherbertEditor && window.__sherbertEditor.ready',
        timeout=30000,
    )
    assert page.evaluate('window.__sherbertEditor.nodeCount(0)') >= 1

    context.close()
