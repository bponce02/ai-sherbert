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


def _text_node_screen_box(page):
    """Screen-pixel bounding box of the first text node on page 0."""
    return page.evaluate(
        """() => {
            const p0 = window.__sherbertEditor.state.pages[0];
            const n = p0.annLayer.getChildren((x) => {
                const m = x.getAttr('sherbert');
                return m && m.type === 'text';
            })[0];
            if (!n) return null;
            const r = n.getClientRect();
            const cont = p0.stage.container().getBoundingClientRect();
            return { x: cont.left + r.x, y: cont.top + r.y, w: r.width, h: r.height };
        }"""
    )


@pytest.mark.django_db(transaction=True)
def test_text_side_resize_wraps_and_corner_resize_scales_font(
    browser, client, live_server, settings, tmp_path
):
    """Text annotations resize per-anchor: middle-right adjusts the wrap-box
    WIDTH (font unchanged), corner anchors scale the font proportionally, and
    the stored width round-trips through a reload."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('resizeowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Text Resize Doc', user=owner)
    pdf_doc.file.save('e2e_resize.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)

    # Create a long-sentence text annotation (auto width => a wide single line).
    page.click('[data-tool="text"]')
    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None
    click_x = box['x'] + box['width'] * 0.2
    click_y = box['y'] + box['height'] * 0.3
    page.mouse.click(click_x, click_y)
    page.wait_for_selector('.sp-text-overlay', timeout=5000)
    page.wait_for_function(
        "document.activeElement && document.activeElement.classList.contains('sp-text-overlay')",
        timeout=5000,
    )
    page.keyboard.type('The quick brown fox jumps over the lazy dog')
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'POST' and r.status == 201,
        timeout=15000,
    ):
        page.keyboard.press('Enter')

    annotation = PDFAnnotation.objects.get(annotation_type='text')
    td = annotation.text_data
    orig_width = td.rect_x2 - td.rect_x1
    orig_font = td.font_size
    assert orig_width > 0

    # Switch to select and click the text node to attach the Transformer.
    page.click('[data-tool="select"]')
    node_box = _text_node_screen_box(page)
    assert node_box is not None
    page.mouse.click(node_box['x'] + 8, node_box['y'] + node_box['h'] * 0.4)
    # Transformer anchors exist once the node is selected.
    page.wait_for_function(
        "() => window.__sherbertEditor.anchorRect('middle-right') !== null",
        timeout=5000,
    )

    # --- Side resize: drag middle-right inward ~40% of the box width. ---
    ar = page.evaluate("() => window.__sherbertEditor.anchorRect('middle-right')")
    drag_dx = node_box['w'] * 0.4
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'PUT',
        timeout=15000,
    ) as put_info:
        page.mouse.move(ar['centerX'], ar['centerY'])
        page.mouse.down()
        page.mouse.move(ar['centerX'] - drag_dx, ar['centerY'], steps=8)
        page.mouse.up()
    assert put_info.value.ok

    td.refresh_from_db()
    side_width = td.rect_x2 - td.rect_x1
    assert side_width < orig_width - 1, (
        f'side resize did not shrink width: {side_width} vs {orig_width}'
    )
    assert td.font_size == orig_font, (
        f'side resize changed font size: {td.font_size} vs {orig_font}'
    )

    # --- Corner resize: drag bottom-right outward => font grows. ---
    cr = page.evaluate("() => window.__sherbertEditor.anchorRect('bottom-right')")
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'PUT',
        timeout=15000,
    ) as put_info2:
        page.mouse.move(cr['centerX'], cr['centerY'])
        page.mouse.down()
        page.mouse.move(cr['centerX'] + 90, cr['centerY'] + 120, steps=8)
        page.mouse.up()
    assert put_info2.value.ok

    td.refresh_from_db()
    assert td.font_size > orig_font, (
        f'corner resize did not grow font: {td.font_size} vs {orig_font}'
    )
    stored_width = td.rect_x2 - td.rect_x1

    # --- Reload: the persisted box width must round-trip onto the Konva node. ---
    page.reload()
    page.wait_for_function(
        'window.__sherbertEditor && window.__sherbertEditor.ready',
        timeout=30000,
    )
    assert page.evaluate('window.__sherbertEditor.nodeCount(0)') >= 1
    node_width = page.evaluate('window.__sherbertEditor.textNodeWidth(0)')
    assert node_width is not None
    assert abs(node_width - stored_width) <= 2, (
        f'reloaded text width {node_width} != stored {stored_width}'
    )

    context.close()


@pytest.mark.django_db(transaction=True)
def test_max_zoom_caps_canvas_backing_store(browser, client, live_server, settings, tmp_path):
    """At max zoom the Konva scene-canvas backing store must stay within the
    16 MP pixel budget (pdf.js maxCanvasPixels) rather than ballooning with
    zoom, and drawing must still work at that scale (pointer coords unaffected
    by the reduced pixel ratio)."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('memowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Zoom Mem Doc', user=owner)
    pdf_doc.file.save('e2e_mem.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)

    page.evaluate('window.__sherbertEditor.setZoom(4)')  # MAX_ZOOM
    assert page.evaluate('window.__sherbertEditor.zoom()') == 4

    # The first #sp-pages canvas is page 0's background scene canvas. Its
    # backing store (width*height device pixels) must respect the budget.
    dims = page.evaluate(
        """() => {
            const c = document.querySelector('#sp-pages canvas');
            return { w: c.width, h: c.height };
        }"""
    )
    budget = 16777216
    assert dims['w'] * dims['h'] <= budget * 1.02, (
        f'scene canvas {dims} = {dims["w"] * dims["h"]}px exceeds 16 MP budget'
    )

    # Drawing still works at max zoom (over the now-large centered page).
    page.click('[data-tool="pen"]')
    scroll_box = page.locator('#sp-scroll').bounding_box()
    sx = scroll_box['x'] + scroll_box['width'] * 0.5
    sy = scroll_box['y'] + scroll_box['height'] * 0.5
    with page.expect_response(
        lambda r: '/api/annotations' in r.url and r.request.method == 'POST' and r.status == 201,
        timeout=15000,
    ):
        page.mouse.move(sx, sy)
        page.mouse.down()
        for i in range(1, 6):
            page.mouse.move(sx + i * 10, sy + i * 6)
        page.mouse.up()
    assert PDFAnnotation.objects.filter(annotation_type='pen').count() >= 1

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


def _doc_point_under(page, cx, cy):
    """Document point (PDF points on page 0) under viewport pixel (cx, cy),
    read against the committed page rect + stage scale."""
    return page.evaluate(
        """([cx, cy]) => {
            const p0 = window.__sherbertEditor.state.pages[0];
            const r = p0.wrap.getBoundingClientRect();
            const scale = p0.stage.scaleX();
            return { x: (cx - r.left) / scale, y: (cy - r.top) / scale };
        }""",
        [cx, cy],
    )


def _screen_of_doc_point(page, px, py):
    """Where document point (px, py) on page 0 currently sits on screen."""
    return page.evaluate(
        """([px, py]) => {
            const p0 = window.__sherbertEditor.state.pages[0];
            const r = p0.wrap.getBoundingClientRect();
            const scale = p0.stage.scaleX();
            return { x: r.left + px * scale, y: r.top + py * scale };
        }""",
        [px, py],
    )


def _assert_no_lingering_transform(page):
    transform = page.evaluate(
        "() => { const el = document.getElementById('sp-pages');"
        " return [el.style.transform, getComputedStyle(el).transform]; }"
    )
    assert transform[0] == '', f'inline transform lingered: {transform[0]!r}'
    assert transform[1] in ('', 'none'), f'computed transform lingered: {transform[1]!r}'


@pytest.mark.django_db(transaction=True)
def test_ctrl_wheel_zoom_per_axis_fitting(browser, client, live_server, settings, tmp_path):
    """pdf.js per-axis zoom, page still FITTING horizontally after the burst.

    The US-Letter page (918x1188px at zoom 1) FITS the 1280px-wide viewport
    and OVERFLOWS its ~672px height. Zooming in a little (end zoom ~1.25) keeps
    the page fitting horizontally, so the horizontal axis must stay CENTERED (no
    drift toward the off-center cursor); the vertical axis overflows, so its
    document point under the cursor must hold (directional)."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('wheelowner', password='pw')
    pdf_doc = PDFDocument(title='E2E Wheel Zoom Doc', user=owner)
    pdf_doc.file.save('e2e_wheel.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)
    assert page.evaluate('window.__sherbertEditor.zoom()') == 1

    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None
    # Cursor deliberately OFF horizontal center (0.75): a wrongly cursor-anchored
    # horizontal axis would drift the page left; correct centering ignores it.
    cx = box['x'] + box['width'] * 0.75
    cy = box['y'] + box['height'] * 0.3

    anchor_pts = _doc_point_under(page, cx, cy)

    # Small zoom-in burst: exp(0.12) per -60 event => ~1.27 after 2 events,
    # still under the horizontal-fit threshold (~1.39 for a 918px page).
    page.mouse.move(cx, cy)
    page.keyboard.down('Control')
    for _ in range(2):
        page.mouse.wheel(0, -60)
    page.keyboard.up('Control')

    page.wait_for_function('() => window.__sherbertEditor.zoom() > 1', timeout=10000)
    end_zoom = page.evaluate('window.__sherbertEditor.zoom()')
    assert 1.0 < end_zoom < 1.35, f'unexpected end zoom {end_zoom}'

    # (a) No CSS transform survives the commit.
    _assert_no_lingering_transform(page)

    # (b) Horizontal: the page wrap stays centered in the viewport.
    centering = page.evaluate(
        """() => {
            const scroll = document.getElementById('sp-scroll');
            const sr = scroll.getBoundingClientRect();
            const r = window.__sherbertEditor.state.pages[0].wrap.getBoundingClientRect();
            return {
                pageCenterX: r.left + r.width / 2,
                viewportCenterX: sr.left + scroll.clientWidth / 2,
            };
        }"""
    )
    assert abs(centering['pageCenterX'] - centering['viewportCenterX']) <= 2, (
        f'horizontal not centered: {centering}'
    )

    # (c) Vertical (overflowing): the document point under the cursor holds.
    screen = _screen_of_doc_point(page, anchor_pts['x'], anchor_pts['y'])
    assert abs(screen['y'] - cy) <= 8, f'vertical anchor drifted: {screen["y"]} vs {cy}'

    # (d) Drawing still works — pointer coordinates were not corrupted. Draw at
    # the (centered) page center, guaranteed on-page and inside the viewport.
    page.click('[data-tool="pen"]')
    sx = centering['viewportCenterX']
    sy = cy
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


@pytest.mark.django_db(transaction=True)
def test_ctrl_wheel_zoom_directional_once_overflowing(browser, client, live_server, settings, tmp_path):
    """Once the page is 'fully covered' on an axis, directional zoom unlocks.

    A bigger zoom-in burst (end zoom well past ~1.4) pushes the 918px page past
    the viewport width, so the HORIZONTAL axis becomes cursor-anchored: the
    document point under the (off-center) cursor must now hold on both axes."""
    settings.MEDIA_ROOT = tmp_path
    owner = User.objects.create_user('wheelowner2', password='pw')
    pdf_doc = PDFDocument(title='E2E Wheel Zoom Doc 2', user=owner)
    pdf_doc.file.save('e2e_wheel2.pdf', ContentFile(make_pdf_bytes()), save=True)

    context, page = _open_editor(browser, client, live_server, settings, pdf_doc)
    assert page.evaluate('window.__sherbertEditor.zoom()') == 1

    canvas = page.locator('#sp-pages canvas').first
    box = canvas.bounding_box()
    assert box is not None
    cx = box['x'] + box['width'] * 0.75
    cy = box['y'] + box['height'] * 0.3

    anchor_pts = _doc_point_under(page, cx, cy)

    # Large burst: exp(0.12) per -60 event => ~2.6 after 8 events, well past the
    # horizontal-fit threshold, so horizontal overflows and goes directional.
    page.mouse.move(cx, cy)
    page.keyboard.down('Control')
    for _ in range(8):
        page.mouse.wheel(0, -60)
    page.keyboard.up('Control')

    page.wait_for_function('() => window.__sherbertEditor.zoom() > 1.4', timeout=10000)
    _assert_no_lingering_transform(page)

    # The document point under the cursor now holds on BOTH axes (directional).
    screen = _screen_of_doc_point(page, anchor_pts['x'], anchor_pts['y'])
    assert abs(screen['x'] - cx) <= 8, f'horizontal anchor drifted: {screen["x"]} vs {cx}'
    assert abs(screen['y'] - cy) <= 8, f'vertical anchor drifted: {screen["y"]} vs {cy}'

    context.close()
