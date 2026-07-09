"""
Django Ninja API endpoints for the sherbert_pdf app.

Exposes a mountable ``ninja.Router``. No auth is attached here — hosts
provide authentication when mounting the router on their own NinjaAPI.
"""
from ninja import Router, UploadedFile
from ninja.errors import HttpError
from django.http import HttpResponse

from .models import (
    PDFDocument,
    PDFAnnotation,
    PenAnnotationData as PenAnnotationModel,
    TextAnnotationData as TextAnnotationModel,
    StampAnnotationData as StampAnnotationModel,
)
from .schemas import (
    PDFDocumentOut,
    AnnotationCreate,
    AnnotationUpdate,
    AnnotationDelete,
    AnnotationOut,
    PenAnnotationData as PenAnnotationSchema,
    TextAnnotationData as TextAnnotationSchema,
    StampAnnotationData as StampAnnotationSchema,
)
from .access import check_pdf_access, check_annotation_ownership


router = Router(tags=["Sherbert PDF"])


def parse_annotation_data(annotation_type: str, data: dict):
    """Convert dict from database to proper Pydantic model based on annotation type."""
    if annotation_type in ['pen', 'highlighter']:
        return PenAnnotationSchema(**data)
    elif annotation_type == 'text':
        return TextAnnotationSchema(**data)
    elif annotation_type == 'stamp':
        return StampAnnotationSchema(**data)
    elif annotation_type == 'cloud':
        return PenAnnotationSchema(**data)
    else:
        raise ValueError(f"Unknown annotation type: {annotation_type}")


# PDF Document Endpoints
@router.post("/pdf-documents", response={201: PDFDocumentOut})
def create_pdf_document(request, pdf_file: UploadedFile, pdf_title: str = "Untitled"):
    """Create a new PDF document with file upload."""
    pdf_doc = PDFDocument.objects.create(
        title=pdf_title,
        file=pdf_file,
        user=request.user,
        quick_edit=True
    )

    return 201, PDFDocumentOut(
        id=pdf_doc.id,
        title=pdf_doc.title,
        file_url=pdf_doc.file.url
    )


@router.get("/pdf-documents/{pdf_id}", response=PDFDocumentOut)
def get_pdf_detail(request, pdf_id: int):
    """Get details of a specific PDF document."""
    has_access, pdf = check_pdf_access(request.user, pdf_document_id=pdf_id)

    if not has_access:
        raise HttpError(404, "PDF document not found")

    return PDFDocumentOut(
        id=pdf.id,
        title=pdf.title,
        file_url=pdf.file.url
    )


@router.get("/pdf-documents/{pdf_id}/export")
def export_pdf(request, pdf_id: int):
    """Export a PDF document with annotations."""
    has_access, pdf_doc = check_pdf_access(request.user, pdf_document_id=pdf_id)

    if not has_access:
        raise HttpError(404, "PDF document not found")

    pdf_bytes = pdf_doc.export()

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{pdf_doc.title}_exported.pdf"'
    return response


# PDF Annotation Endpoints
@router.post("/annotations", response={201: AnnotationOut})
def create_annotation(request, data: AnnotationCreate):
    """Create a new annotation on a PDF document."""
    has_access, pdf_doc = check_pdf_access(
        request.user,
        pdf_document_id=data.pdf_document_id
    )

    if not has_access:
        raise HttpError(404, "PDF document not found")

    annot_data = data.annotation.annotation_data
    annot_type = data.annotation.annotation_type

    # Extract color data (not all annotation types have colors)
    if hasattr(annot_data, 'colors') and annot_data.colors:
        colors = annot_data.colors.stroke
        color_r, color_g, color_b = colors[0], colors[1], colors[2]
    else:
        color_r, color_g, color_b = 0, 0, 0

    # Create the base annotation
    annotation = PDFAnnotation.objects.create(
        pdf_document=pdf_doc,
        user=request.user,
        page_number=data.annotation.page_number,
        annotation_type=annot_type,
        color_r=color_r,
        color_g=color_g,
        color_b=color_b
    )

    # Create the type-specific data model
    if annot_type in ['pen', 'highlighter']:
        PenAnnotationModel.objects.create(
            annotation=annotation,
            vertices=annot_data.vertices,
            border_width=annot_data.border.width,
            opacity=annot_data.opacity
        )
    elif annot_type == 'text':
        TextAnnotationModel.objects.create(
            annotation=annotation,
            rect_x1=annot_data.rect[0],
            rect_y1=annot_data.rect[1],
            rect_x2=annot_data.rect[2],
            rect_y2=annot_data.rect[3],
            content=annot_data.content,
            font_size=annot_data.fontSize,
            font_family=annot_data.fontFamily,
            font_style=annot_data.fontStyle,
        )
    elif annot_type == 'stamp':
        StampAnnotationModel.objects.create(
            annotation=annotation,
            x=annot_data.x,
            y=annot_data.y,
            width=annot_data.width,
            height=annot_data.height,
            image_url=annot_data.imageUrl
        )
    elif annot_type == 'cloud':
        PenAnnotationModel.objects.create(
            annotation=annotation,
            vertices=annot_data.vertices,
            border_width=annot_data.border.width,
            opacity=annot_data.opacity
        )

    return 201, AnnotationOut(
        id=annotation.id,
        page_number=annotation.page_number,
        annotation_type=annotation.annotation_type,
        annotation_data=data.annotation.annotation_data,
        user_id=annotation.user_id,
        is_owner=True,
        user_name=request.user.get_full_name() or request.user.username
    )


@router.put("/annotations", response={204: None})
def update_annotation(request, data: AnnotationUpdate):
    """Update an existing annotation."""
    is_owner, annotation, error_msg = check_annotation_ownership(
        request.user,
        annotation_id=data.annotation_id
    )

    if not is_owner:
        status_code = 404 if error_msg == 'Annotation not found' else 403
        raise HttpError(status_code, error_msg)

    annot_data = data.annotation_data

    # Update color data (only for annotations that have colors)
    if hasattr(annot_data, 'colors') and annot_data.colors:
        colors = annot_data.colors.stroke
        annotation.color_r = colors[0]
        annotation.color_g = colors[1]
        annotation.color_b = colors[2]
        annotation.save()

    # Update type-specific data
    if annotation.annotation_type in ['pen', 'highlighter']:
        pen_data = annotation.pen_data
        pen_data.vertices = annot_data.vertices
        pen_data.border_width = annot_data.border.width
        pen_data.opacity = annot_data.opacity
        pen_data.erasures = [e.model_dump() for e in annot_data.erasures] if annot_data.erasures else []
        pen_data.save()
    elif annotation.annotation_type == 'text':
        text_data = annotation.text_data
        text_data.rect_x1 = annot_data.rect[0]
        text_data.rect_y1 = annot_data.rect[1]
        text_data.rect_x2 = annot_data.rect[2]
        text_data.rect_y2 = annot_data.rect[3]
        text_data.content = annot_data.content
        text_data.font_size = annot_data.fontSize
        text_data.font_family = annot_data.fontFamily
        text_data.font_style = annot_data.fontStyle
        text_data.save()
    elif annotation.annotation_type == 'stamp':
        stamp_data = annotation.stamp_data
        stamp_data.x = annot_data.x
        stamp_data.y = annot_data.y
        stamp_data.width = annot_data.width
        stamp_data.height = annot_data.height
        stamp_data.image_url = annot_data.imageUrl
        stamp_data.save()
    elif annotation.annotation_type == 'cloud':
        pen_data = annotation.pen_data
        pen_data.vertices = annot_data.vertices
        pen_data.border_width = annot_data.border.width
        pen_data.opacity = annot_data.opacity
        pen_data.save()

    return 204, None


@router.delete("/annotations", response={204: None})
def delete_annotation(request, data: AnnotationDelete):
    """Delete an annotation."""
    is_owner, annotation, error_msg = check_annotation_ownership(
        request.user,
        annotation_id=data.annotation_id
    )

    if not is_owner:
        status_code = 404 if error_msg == 'Annotation not found' else 403
        raise HttpError(status_code, error_msg)

    annotation.delete()

    return 204, None


@router.get("/pdf-documents/{pdf_id}/annotations", response=list[AnnotationOut])
def list_annotations(request, pdf_id: int):
    """Get all annotations for a PDF document."""
    has_access, _ = check_pdf_access(request.user, pdf_document_id=pdf_id)

    if not has_access:
        raise HttpError(403, "Access denied")

    annotations = PDFAnnotation.objects.filter(
        pdf_document_id=pdf_id
    ).select_related('user', 'pen_data', 'text_data', 'stamp_data')

    return [
        AnnotationOut(
            id=annot.id,
            page_number=annot.page_number,
            annotation_type=annot.annotation_type,
            annotation_data=parse_annotation_data(annot.annotation_type, annot.get_annotation_data()),
            user_id=annot.user_id,
            is_owner=annot.user_id == request.user.id,
            user_name=annot.user.get_full_name() if annot.user else 'Unknown'
        )
        for annot in annotations
    ]
