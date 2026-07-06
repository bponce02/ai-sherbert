"""
Pluggable access policy for sherbert_pdf.

Host projects can replace the default owner-only policy by pointing the
``SHERBERT_PDF_ACCESS_POLICY`` setting at a dotted path to an
``AccessPolicy`` subclass (or any class with the same interface).

The module-level ``check_pdf_access`` / ``check_annotation_ownership``
functions keep the exact signatures, return shapes, and error message
strings of CoreCRM's ``doc.views`` originals; they handle lookup and
DoesNotExist, then delegate the actual access decision to the policy.
"""
from django.conf import settings
from django.utils.module_loading import import_string

from .models import PDFAnnotation, PDFDocument


class AccessPolicy:
    """Default policy: only the owning user may access a document, and
    only the annotation's author (who also has document access) may
    modify an annotation."""

    def can_access_document(self, user, pdf_document) -> bool:
        """Return True if user may view/annotate the PDF document."""
        return pdf_document.user == user

    def can_modify_annotation(self, user, annotation):
        """Return (allowed, error_message or None) for modifying an annotation."""
        if not self.can_access_document(user, annotation.pdf_document):
            return False, 'Access denied to PDF document'

        if annotation.user_id != user.id:
            return False, 'You can only modify your own annotations'

        return True, None


def get_access_policy() -> AccessPolicy:
    """Resolve the active access policy.

    Reads ``settings.SHERBERT_PDF_ACCESS_POLICY`` (an optional dotted path)
    and instantiates it on every call — no module-level caching, so
    ``override_settings`` remains testable. Falls back to ``AccessPolicy``.
    """
    dotted_path = getattr(settings, 'SHERBERT_PDF_ACCESS_POLICY', None)
    policy_class = import_string(dotted_path) if dotted_path else AccessPolicy
    return policy_class()


def check_pdf_access(user, pdf_document_id=None, pdf_document=None):
    """
    Check if user has access to a PDF document.

    Args:
        user: The user to check access for
        pdf_document_id: ID of the PDF document (optional if pdf_document provided)
        pdf_document: PDFDocument instance (optional if pdf_document_id provided)

    Returns:
        tuple: (has_access: bool, pdf_document: PDFDocument or None)
    """
    if pdf_document is None and pdf_document_id is None:
        return False, None

    # Get the PDF document if only ID provided
    if pdf_document is None:
        try:
            pdf_document = PDFDocument.objects.get(id=pdf_document_id)
        except PDFDocument.DoesNotExist:
            return False, None

    return get_access_policy().can_access_document(user, pdf_document), pdf_document


def check_annotation_ownership(user, annotation_id=None, annotation=None):
    """
    Check if user owns a specific annotation AND has access to the PDF.

    Args:
        user: The user to check ownership for
        annotation_id: ID of the annotation (optional if annotation provided)
        annotation: PDFAnnotation instance (optional if annotation_id provided)

    Returns:
        tuple: (is_owner: bool, annotation: PDFAnnotation or None, error_message: str or None)
    """
    if annotation is None and annotation_id is None:
        return False, None, 'No annotation specified'

    if annotation is None:
        try:
            annotation = PDFAnnotation.objects.select_related('pdf_document').get(id=annotation_id)
        except PDFAnnotation.DoesNotExist:
            return False, None, 'Annotation not found'

    allowed, error_message = get_access_policy().can_modify_annotation(user, annotation)
    return allowed, annotation, error_message
