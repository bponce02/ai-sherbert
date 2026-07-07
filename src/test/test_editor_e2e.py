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


def _open_editor(browser, client, live_server, settings, pdf_doc):
    """Log in, inject the session cookie, load the editor, and wait for ready."""
    client.force_login(pdf_doc.user)
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
    return context, page


@pytest.mark.django_db(transaction=True)
def test_text_annotation_persists_and_rerenders(browser, client, live_server, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('textowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Text Doc', user=owner)
    pdf_doc.file.save('e2e_text.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)

    page.click('[data-tool="text"]')

    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None
    click_x = box['x'] + box['width'] * 0.3
    click_y = box['y'] + box['height'] * 0.3
    page.mouse.click(click_x, click_y)

    # The overlay textarea is created synchronously, but focus is deferred
    # one animation frame (and reclaimed if the opening click blurs it).
    page.wait_for_selector('.sp-text-overlay', timeout=5000)
    page.wait_for_function(
        "document.activeElement && document.activeElement.classList.contains('sp-text-overlay')",
        timeout=5000,
    )

    page.keyboard.type('Hello E2E')
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'POST' and r.status == 201,
        timeout=15000,
    ):
        page.keyboard.press('Enter')

    annotation = PDFAnnotation.objects.get(annotation_type='text')
    assert annotation.pdf_document_id == pdf_doc.pk
    assert annotation.text_data.content == 'Hello E2E'

    # Reload: the text node must come back from the API.
    page.reload()
    page.wait_for_function(
        'window.__sherbertEditor && window.__sherbertEditor.ready',
        timeout=30000,
    )
    assert page.evaluate('window.__sherbertEditor.nodeCount(0)') >= 1

    context.close()


@pytest.mark.django_db(transaction=True)
def test_zoom_rerenders_bitmap_at_higher_scale(browser, client, live_server, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('zoomowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Zoom Doc', user=owner)
    pdf_doc.file.save('e2e_zoom.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)

    # Initial bitmap scale is RENDER_SCALE(1.5) * devicePixelRatio (1 headless).
    initial_scale = page.evaluate('window.__sherbertEditor.bitmapScale(0)')
    assert initial_scale is not None

    page.evaluate('window.__sherbertEditor.setZoom(3)')

    # The re-render is debounced (~220ms) and visible-pages-only; page 0 is
    # in view, so its bitmap should sharpen to a higher resolution.
    page.wait_for_function(
        '(init) => window.__sherbertEditor.bitmapScale(0) > init',
        arg=initial_scale,
        timeout=10000,
    )
    assert page.evaluate('window.__sherbertEditor.zoom()') == 3

    context.close()


@pytest.mark.django_db(transaction=True)
def test_ctrl_wheel_zoom_previews_then_commits_cleanly(browser, client, live_server, settings, tmp_path):
    """A burst of ctrl+wheel events must preview via CSS, then commit once —
    leaving no lingering transform and no corrupted pointer coordinates."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('wheelowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Wheel Zoom Doc', user=owner)
    pdf_doc.file.save('e2e_wheel.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)

    assert page.evaluate('window.__sherbertEditor.zoom()') == 1

    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None
    cx = box['x'] + box['width'] * 0.5
    cy = box['y'] + box['height'] * 0.5

    # Fire a rapid burst of ctrl+wheel zoom-in events (negative deltaY).
    page.mouse.move(cx, cy)
    page.keyboard.down('Control')
    for _ in range(8):
        page.mouse.wheel(0, -60)
    page.keyboard.up('Control')

    # (b) Phase-2 commit is debounced (~180ms); the committed zoom changes from 1.
    page.wait_for_function('() => window.__sherbertEditor.zoom() > 1', timeout=10000)

    # (a) No CSS transform survives the commit on #sp-pages.
    transform = page.evaluate(
        "() => { const el = document.getElementById('sp-pages');"
        " return [el.style.transform, getComputedStyle(el).transform]; }"
    )
    assert transform[0] == '', f'inline transform lingered: {transform[0]!r}'
    assert transform[1] in ('', 'none'), f'computed transform lingered: {transform[1]!r}'

    # (c) Drawing still works after the gesture — coordinates were not corrupted.
    # The anchor point (cx, cy) is held stationary through the commit, so it is
    # guaranteed to still be over the (now larger) page and inside the viewport.
    page.click('[data-tool="pen"]')
    sx = cx - 40
    sy = cy - 20
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'POST' and r.status == 201,
        timeout=15000,
    ):
        page.mouse.move(sx, sy)
        page.mouse.down()
        for i in range(1, 6):
            page.mouse.move(sx + i * 15, sy + i * 8)
        page.mouse.up()

    assert PDFAnnotation.objects.filter(annotation_type='pen').count() >= 1

    context.close()
