import html
import logging
import os

import pymupdf
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


class BaseModel(models.Model):
    """Abstract base model replicating CoreCRM's utils.models.BaseModel
    column-for-column, so existing doc_* tables can be handed over to
    these models without schema changes."""
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)

    class Meta:
        abstract = True


def _generate_cloud_points(x, y, width, height, scallop=35, steps=8):
    """Generate scalloped cloud outline points from a bounding rect.
    Mirrors the JS generateCloudPath: each scallop is a circular arc (SVG A r r 0 0 1).
    We find the arc center and sample true circular arc points so the export matches."""
    import math

    h_count = max(2, round(width / scallop))
    v_count = max(2, round(height / scallop))
    h_step, v_step = width / h_count, height / v_count
    hr, vr = h_step / 2, v_step / 2

    def arc_pts(x0, y0, r, x1, y1):
        """Sample points along a circular arc from (x0,y0) to (x1,y1) with radius r, sweep=1."""
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        dx, dy = x1 - x0, y1 - y0
        chord = math.hypot(dx, dy)
        if chord == 0 or r <= 0:
            return [(x1, y1)]
        # Clamp r so arc is valid
        r = max(r, chord / 2)
        # Distance from midpoint to center
        d = math.sqrt(r * r - (chord / 2) ** 2)
        # Unit normal (sweep=1 in SVG y-down: center is to the left of chord direction)
        nx, ny = dy / chord, -dx / chord
        cx_arc = mx + nx * d
        cy_arc = my + ny * d
        # Angles from center to start and end
        a0 = math.atan2(y0 - cy_arc, x0 - cx_arc)
        a1 = math.atan2(y1 - cy_arc, x1 - cx_arc)
        # Sweep=1 is clockwise in SVG (y-down), meaning increasing angle
        da = a1 - a0
        if da < 0:
            da += 2 * math.pi
        pts = []
        for s in range(1, steps + 1):
            a = a0 + da * s / steps
            pts.append((cx_arc + r * math.cos(a), cy_arc + r * math.sin(a)))
        return pts

    pts = [(x, y)]
    prev = (x, y)
    for edges, radius in [
        ([(x + (i + 1) * h_step, y) for i in range(h_count)], hr),
        ([(x + width, y + (i + 1) * v_step) for i in range(v_count)], vr),
        ([(x + width - (i + 1) * h_step, y + height) for i in range(h_count)], hr),
        ([(x, y + height - (i + 1) * v_step) for i in range(v_count)], vr),
    ]:
        for ex, ey in edges:
            pts.extend(arc_pts(prev[0], prev[1], radius, ex, ey))
            prev = (ex, ey)
    pts.append((x, y))
    return pts


ANNOTATION_TYPES = [
    ('pen', 'Pen'),
    ('highlighter', 'Highlighter'),
    ('text', 'Text'),
    ('stamp', 'Stamp'),
    ('cloud', 'Cloud'),
]


def export_pdf(pdf_document):
    """Render annotations from database onto PDF using MuPDF native methods"""
    pdf = pymupdf.open(pdf_document.file.path)

    annotations = pdf_document.annotations.select_related('pen_data', 'text_data', 'stamp_data').all()

    for annotation in annotations:
        try:
            page = pdf[annotation.page_number]
            data = annotation.get_annotation_data()

            if annotation.annotation_type in ['pen', 'highlighter']:
                vertices = data.get('vertices', [])
                if not vertices or not vertices[0] or len(vertices[0]) < 2:
                    continue

                points = [(pt[0], pt[1]) for pt in vertices[0]]
                annot = page.add_ink_annot([points])

                colors = data.get('colors', {})
                if 'stroke' in colors:
                    stroke_color = tuple(colors['stroke'])
                    annot.set_colors(stroke=stroke_color)

                border = data.get('border', {})
                if 'width' in border:
                    annot.set_border(width=border['width'])

                opacity = data.get('opacity', 1.0)
                if annotation.annotation_type == 'highlighter':
                    opacity = 0.3
                annot.set_opacity(opacity)

                annot.update()

            elif annotation.annotation_type == 'text':
                rect_data = data.get('rect', [0, 0, 100, 50])
                rect = pymupdf.Rect(rect_data[0], rect_data[1], rect_data[2], rect_data[3])
                content = data.get('content', '')

                colors = data.get('colors', {})
                color_str = 'black'
                text_color = (0, 0, 0)
                if 'stroke' in colors:
                    rgb = colors['stroke']
                    color_str = f"rgb({int(rgb[0]*255)}, {int(rgb[1]*255)}, {int(rgb[2]*255)})"
                    text_color = (rgb[0], rgb[1], rgb[2])

                font_size = data.get('fontSize', 12)
                font_family = data.get('fontFamily', 'Arial, sans-serif')
                font_weight = data.get('fontWeight', 400)

                # Check if this is a signature (cursive font)
                is_signature = 'cursive' in font_family.lower() or 'script' in font_family.lower()

                line_height = data.get('lineHeight', 1.1)
                letter_spacing = data.get('letterSpacing', 'normal')

                # Use italic for signatures
                font_style = 'italic' if is_signature else 'normal'

                escaped = html.escape(content).replace('\n', '<br>')
                html_snippet = (
                    f"<div style='color: {color_str}; "
                    f"font-size: {int(font_size)}pt; "
                    f"font-family: Arial, sans-serif; "
                    f"font-style: {font_style}; "
                    f"font-weight: {font_weight}; "
                    f"line-height: {line_height}; "
                    f"letter-spacing: {letter_spacing}; "
                    f"text-align: left; "
                    f"text-decoration: none; "
                    f"text-transform: none; "
                    f"word-spacing: normal; "
                    f"text-indent: 0;'>"
                    f"{escaped}</div>"
                )
                page.insert_htmlbox(rect, html_snippet)

            elif annotation.annotation_type == 'stamp' and data.get('type') == 'stamp':
                x = float(data.get('x', 0))
                y = float(data.get('y', 0))
                width = float(data.get('width', 0))
                height = float(data.get('height', 0))
                image_url = data.get('imageUrl', '')

                if not image_url or width == 0 or height == 0:
                    continue

                rect = pymupdf.Rect(x, y, x + width, y + height)

                image_path = None

                url_path = image_url

                # If the frontend stored a full URL, strip scheme/host and STATIC_URL prefix.
                try:
                    if url_path.startswith('http://') or url_path.startswith('https://'):
                        from urllib.parse import urlparse

                        parsed = urlparse(url_path)
                        url_path = parsed.path or ''
                except Exception:
                    pass

                static_url = getattr(settings, 'STATIC_URL', '') or ''
                if static_url and url_path.startswith(static_url):
                    url_path = url_path[len(static_url):]

                if url_path.startswith('/'):
                    url_path = url_path[1:]

                static_root = getattr(settings, 'STATIC_ROOT', '') or ''
                base_dir = getattr(settings, 'BASE_DIR', None)

                candidates = []
                if static_root:
                    candidates.append(os.path.join(static_root, url_path))
                if base_dir:
                    candidates.append(os.path.join(base_dir, 'static', url_path))

                for candidate in candidates:
                    if os.path.exists(candidate):
                        image_path = candidate
                        break

                if image_path and os.path.exists(image_path):
                    page.insert_image(rect, filename=image_path)
                else:
                    logger.warning(f"Could not find stamp image. URL: {image_url}, Candidates: {candidates}")

            elif annotation.annotation_type == 'cloud':
                vertices = data.get('vertices', [])
                if not vertices or not vertices[0]:
                    continue
                rect = vertices[0]
                # New format: single [x,y,w,h] rect stored as vertices[0][0]
                if len(rect) == 1 and len(rect[0]) == 4:
                    rx, ry, rw, rh = rect[0]
                    points = _generate_cloud_points(rx, ry, rw, rh)
                else:
                    # Legacy: array of [x,y] points
                    if len(rect) < 2:
                        continue
                    points = [(pt[0], pt[1]) for pt in rect]
                annot = page.add_ink_annot([points])

                colors = data.get('colors', {})
                if 'stroke' in colors:
                    stroke_color = tuple(colors['stroke'])
                    annot.set_colors(stroke=stroke_color)

                border = data.get('border', {})
                if 'width' in border:
                    annot.set_border(width=border['width'])

                annot.set_opacity(data.get('opacity', 1.0))
                annot.update()

        except Exception as e:
            logger.warning(f"Error processing annotation: {e}")
            continue

    pdf_bytes = pdf.tobytes(garbage=4, deflate=True)
    pdf.close()

    return pdf_bytes


class PDFDocument(BaseModel):
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='pdfs/', blank=True, null=True)
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, null=True, blank=True)
    quick_edit = models.BooleanField(default=False)

    def export(self):
        return export_pdf(self)


class PDFAnnotation(BaseModel):
    pdf_document = models.ForeignKey(PDFDocument, on_delete=models.CASCADE, related_name='annotations')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, blank=True, null=True)
    page_number = models.IntegerField(help_text='0-indexed page number for MuPDF compatibility')
    annotation_type = models.CharField(max_length=20, choices=ANNOTATION_TYPES)

    # Color data (RGB as 3 floats 0.0-1.0)
    color_r = models.FloatField(null=True, blank=True)
    color_g = models.FloatField(null=True, blank=True)
    color_b = models.FloatField(null=True, blank=True)

    def get_annotation_data(self):
        """Get annotation data as a properly structured dict based on type."""
        if self.annotation_type in ['pen', 'highlighter']:
            pen_data = self.pen_data
            if not pen_data:
                return {}
            result = {
                'vertices': pen_data.vertices,
                'colors': {'stroke': [self.color_r, self.color_g, self.color_b]},
                'border': {'width': pen_data.border_width},
                'opacity': pen_data.opacity
            }
            if pen_data.erasures:
                result['erasures'] = pen_data.erasures
            return result
        elif self.annotation_type == 'text':
            text_data = self.text_data
            if not text_data:
                return {}
            return {
                'rect': [text_data.rect_x1, text_data.rect_y1, text_data.rect_x2, text_data.rect_y2],
                'content': text_data.content,
                'colors': {'stroke': [self.color_r, self.color_g, self.color_b]},
                'fontSize': text_data.font_size,
                'fontFamily': text_data.font_family,
                'fontStyle': text_data.font_style,
            }
        elif self.annotation_type == 'stamp':
            stamp_data = self.stamp_data
            if not stamp_data:
                return {}
            return {
                'type': 'stamp',
                'x': stamp_data.x,
                'y': stamp_data.y,
                'width': stamp_data.width,
                'height': stamp_data.height,
                'imageUrl': stamp_data.image_url
            }
        elif self.annotation_type == 'cloud':
            pen_data = self.pen_data
            if not pen_data:
                return {}
            result = {
                'vertices': pen_data.vertices,
                'colors': {'stroke': [self.color_r, self.color_g, self.color_b]},
                'border': {'width': pen_data.border_width},
                'opacity': pen_data.opacity
            }
            if pen_data.erasures:
                result['erasures'] = pen_data.erasures
            return result
        return {}


class PenAnnotationData(BaseModel):
    annotation = models.OneToOneField(PDFAnnotation, on_delete=models.CASCADE, related_name='pen_data')
    vertices = models.JSONField(help_text='Array of points: [[[x1,y1], [x2,y2], ...]]')
    border_width = models.FloatField(default=1.0)
    opacity = models.FloatField(default=1.0)
    erasures = models.JSONField(default=list, blank=True, help_text='Eraser circles in PDF coords: [{cx, cy, r}]')


class TextAnnotationData(BaseModel):
    annotation = models.OneToOneField(PDFAnnotation, on_delete=models.CASCADE, related_name='text_data')
    rect_x1 = models.FloatField()
    rect_y1 = models.FloatField()
    rect_x2 = models.FloatField()
    rect_y2 = models.FloatField()
    content = models.TextField()
    font_size = models.IntegerField(default=16)
    font_family = models.CharField(max_length=255, default='Arial, sans-serif')
    font_style = models.CharField(max_length=20, default='normal')


class StampAnnotationData(BaseModel):
    annotation = models.OneToOneField(PDFAnnotation, on_delete=models.CASCADE, related_name='stamp_data')
    x = models.FloatField()
    y = models.FloatField()
    width = models.FloatField()
    height = models.FloatField()
    image_url = models.CharField(max_length=500)
