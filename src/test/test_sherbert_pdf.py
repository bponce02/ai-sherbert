import pytest
import pymupdf

from sherbert_pdf.models import (
    PDFDocument,
    PDFAnnotation,
    PenAnnotationData,
    TextAnnotationData,
    StampAnnotationData,
    export_pdf,
    _apply_erasures,
    _generate_cloud_points,
)


def _seed_pdf_file(document, tmp_path, name='source.pdf'):
    """Write a one-page PDF under MEDIA_ROOT and point the document at it."""
    pdf_dir = tmp_path / 'pdfs'
    pdf_dir.mkdir(exist_ok=True)
    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf_dir / name)
    src.close()
    document.file.name = f'pdfs/{name}'
    document.save()


@pytest.fixture
def document(db):
    return PDFDocument.objects.create(title='Test Document')


def _make_pen(document, annotation_type='pen', vertices=None, erasures=None,
              color=(1.0, 0.0, 0.0), border_width=2.0, opacity=1.0):
    annotation = PDFAnnotation.objects.create(
        pdf_document=document,
        page_number=0,
        annotation_type=annotation_type,
        color_r=color[0], color_g=color[1], color_b=color[2],
    )
    PenAnnotationData.objects.create(
        annotation=annotation,
        vertices=vertices if vertices is not None else [[[10, 10], [50, 50], [90, 30]]],
        border_width=border_width,
        opacity=opacity,
        erasures=erasures if erasures is not None else [],
    )
    return annotation


@pytest.mark.django_db
class TestAnnotationDataRoundTrip:
    def test_pen(self, document):
        annotation = _make_pen(document, 'pen')
        assert annotation.get_annotation_data() == {
            'vertices': [[[10, 10], [50, 50], [90, 30]]],
            'colors': {'stroke': [1.0, 0.0, 0.0]},
            'border': {'width': 2.0},
            'opacity': 1.0,
        }

    def test_pen_with_erasures(self, document):
        annotation = _make_pen(document, 'pen', erasures=[{'cx': 20, 'cy': 20, 'r': 5}])
        data = annotation.get_annotation_data()
        assert data['erasures'] == [{'cx': 20, 'cy': 20, 'r': 5}]

    def test_highlighter(self, document):
        annotation = _make_pen(
            document, 'highlighter',
            vertices=[[[0, 100], [200, 100]]],
            color=(1.0, 1.0, 0.0), border_width=12.0, opacity=0.3,
        )
        assert annotation.get_annotation_data() == {
            'vertices': [[[0, 100], [200, 100]]],
            'colors': {'stroke': [1.0, 1.0, 0.0]},
            'border': {'width': 12.0},
            'opacity': 0.3,
        }

    def test_text(self, document):
        annotation = PDFAnnotation.objects.create(
            pdf_document=document,
            page_number=0,
            annotation_type='text',
            color_r=0.0, color_g=0.0, color_b=1.0,
        )
        TextAnnotationData.objects.create(
            annotation=annotation,
            rect_x1=50.0, rect_y1=60.0, rect_x2=250.0, rect_y2=120.0,
            content='Hello, world!',
            font_size=14,
            font_family='Arial, sans-serif',
            font_style='normal',
        )
        assert annotation.get_annotation_data() == {
            'rect': [50.0, 60.0, 250.0, 120.0],
            'content': 'Hello, world!',
            'colors': {'stroke': [0.0, 0.0, 1.0]},
            'fontSize': 14,
            'fontFamily': 'Arial, sans-serif',
            'fontStyle': 'normal',
        }

    def test_stamp(self, document):
        annotation = PDFAnnotation.objects.create(
            pdf_document=document,
            page_number=0,
            annotation_type='stamp',
        )
        StampAnnotationData.objects.create(
            annotation=annotation,
            x=100.0, y=150.0, width=80.0, height=40.0,
            image_url='/static/img/approved-stamp.png',
        )
        assert annotation.get_annotation_data() == {
            'type': 'stamp',
            'x': 100.0,
            'y': 150.0,
            'width': 80.0,
            'height': 40.0,
            'imageUrl': '/static/img/approved-stamp.png',
        }

    def test_cloud(self, document):
        # New format: single [x,y,w,h] rect stored as vertices[0][0]
        annotation = _make_pen(
            document, 'cloud',
            vertices=[[[100, 100, 200, 150]]],
            color=(1.0, 0.0, 0.0), border_width=2.0, opacity=1.0,
        )
        assert annotation.get_annotation_data() == {
            'vertices': [[[100, 100, 200, 150]]],
            'colors': {'stroke': [1.0, 0.0, 0.0]},
            'border': {'width': 2.0},
            'opacity': 1.0,
        }


def test_generate_cloud_points_closed_loop():
    points = _generate_cloud_points(100, 100, 200, 150)
    assert points[0] == (100, 100)
    assert points[-1] == (100, 100)
    assert len(points) > 4


@pytest.mark.django_db
def test_export_pdf_smoke(document, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path

    # Generate a one-page PDF on disk under MEDIA_ROOT.
    pdf_dir = tmp_path / 'pdfs'
    pdf_dir.mkdir()
    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf_dir / 'source.pdf')
    src.close()
    document.file.name = 'pdfs/source.pdf'
    document.save()

    # Pen annotation
    _make_pen(document, 'pen', vertices=[[[10, 10], [50, 50], [90, 30]]])

    # Cloud annotation (new [x,y,w,h] rect format)
    _make_pen(document, 'cloud', vertices=[[[100, 100, 200, 150]]])

    # Text annotation
    text_annotation = PDFAnnotation.objects.create(
        pdf_document=document,
        page_number=0,
        annotation_type='text',
        color_r=0.0, color_g=0.0, color_b=0.0,
    )
    TextAnnotationData.objects.create(
        annotation=text_annotation,
        rect_x1=50.0, rect_y1=400.0, rect_x2=400.0, rect_y2=470.0,
        content='Exported annotation text',
    )

    pdf_bytes = export_pdf(document)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b'%PDF')

    result = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = result[0]

    ink_annots = [a for a in page.annots() if a.type[0] == pymupdf.PDF_ANNOT_INK]
    assert len(ink_annots) == 2, 'expected pen and cloud ink annotations'

    assert 'Exported annotation text' in page.get_text()
    result.close()


@pytest.mark.django_db
def test_export_stamp_resolves_package_static_via_finders(document, settings, tmp_path):
    """A stamp whose imageUrl points at a package-static image (as shipped by
    SHERBERT_PDF_STAMPS / default_stamps) must export in development where no
    collectstatic has run: the staticfiles finders resolve it. The exported
    page must then carry the embedded image."""
    settings.MEDIA_ROOT = tmp_path

    pdf_dir = tmp_path / 'pdfs'
    pdf_dir.mkdir()
    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf_dir / 'stamp_source.pdf')
    src.close()
    document.file.name = 'pdfs/stamp_source.pdf'
    document.save()

    from django.templatetags.static import static

    stamp_url = static('sherbert_pdf/stamps/approved.png')
    assert stamp_url.endswith('sherbert_pdf/stamps/approved.png')

    annotation = PDFAnnotation.objects.create(
        pdf_document=document,
        page_number=0,
        annotation_type='stamp',
    )
    StampAnnotationData.objects.create(
        annotation=annotation,
        x=100.0, y=150.0, width=120.0, height=48.0,
        image_url=stamp_url,
    )

    pdf_bytes = export_pdf(document)
    assert pdf_bytes.startswith(b'%PDF')

    result = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = result[0]
    assert page.get_images(), 'stamp image was not embedded into the exported page'
    result.close()


def test_apply_erasures_splits_stroke_through_circle():
    # Horizontal stroke y=0 from x=0..100 through a circle at (50,0) r=10.
    strokes = _apply_erasures([[(0, 0), (100, 0)]], [{'cx': 50, 'cy': 0, 'r': 10}])
    assert len(strokes) == 2
    left, right = strokes
    # Left run ends where it enters the circle (~x=40), right run starts where
    # it exits (~x=60); the gap straddles the erased circle.
    assert left[0] == (0, 0)
    assert abs(left[-1][0] - 40) < 1e-6
    assert abs(right[0][0] - 60) < 1e-6
    assert right[-1] == (100, 0)


def test_apply_erasures_drops_fully_covered_stroke():
    strokes = _apply_erasures([[(45, 0), (55, 0)]], [{'cx': 50, 'cy': 0, 'r': 100}])
    assert strokes == []


def test_apply_erasures_leaves_untouched_stroke():
    strokes = _apply_erasures([[(0, 0), (100, 0)]], [{'cx': 50, 'cy': 500, 'r': 10}])
    assert strokes == [[(0, 0), (100, 0)]]


@pytest.mark.django_db
def test_export_applies_erasures_to_pen_stroke(document, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _seed_pdf_file(document, tmp_path)

    # Straight 2-point stroke through one erasure circle.
    _make_pen(
        document, 'pen',
        vertices=[[[0, 400], [100, 400]]],
        erasures=[{'cx': 50, 'cy': 400, 'r': 10}],
    )

    pdf_bytes = export_pdf(document)
    result = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = result[0]
    ink_annots = [a for a in page.annots() if a.type[0] == pymupdf.PDF_ANNOT_INK]
    assert len(ink_annots) == 1

    strokes = ink_annots[0].vertices
    # Exactly two disjoint sub-strokes with a gap covering the circle.
    assert len(strokes) == 2
    left, right = strokes
    left_max_x = max(p[0] for p in left)
    right_min_x = min(p[0] for p in right)
    assert left_max_x < 50 < right_min_x
    # The gap spans (roughly) the circle diameter.
    assert (right_min_x - left_max_x) >= 15
    result.close()


@pytest.mark.django_db
def test_export_drops_fully_erased_stroke(document, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _seed_pdf_file(document, tmp_path)

    _make_pen(
        document, 'pen',
        vertices=[[[45, 400], [55, 400]]],
        erasures=[{'cx': 50, 'cy': 400, 'r': 100}],
    )

    pdf_bytes = export_pdf(document)
    result = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = result[0]
    ink_annots = [a for a in page.annots() if a.type[0] == pymupdf.PDF_ANNOT_INK]
    assert ink_annots == []
    result.close()


@pytest.mark.django_db
def test_export_stroke_without_erasure_touch_unchanged(document, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _seed_pdf_file(document, tmp_path)

    _make_pen(
        document, 'pen',
        vertices=[[[0, 400], [100, 400]]],
        erasures=[{'cx': 50, 'cy': 700, 'r': 10}],  # far from the stroke
    )

    pdf_bytes = export_pdf(document)
    result = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = result[0]
    ink_annots = [a for a in page.annots() if a.type[0] == pymupdf.PDF_ANNOT_INK]
    assert len(ink_annots) == 1
    strokes = ink_annots[0].vertices
    assert len(strokes) == 1
    xs = [p[0] for p in strokes[0]]
    assert min(xs) < 1 and max(xs) > 99  # endpoints preserved
    result.close()


@pytest.mark.django_db
def test_pdfdocument_export_method(document, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    pdf_dir = tmp_path / 'pdfs'
    pdf_dir.mkdir()
    src = pymupdf.open()
    src.new_page()
    src.save(pdf_dir / 'plain.pdf')
    src.close()
    document.file.name = 'pdfs/plain.pdf'
    document.save()

    assert document.export().startswith(b'%PDF')
