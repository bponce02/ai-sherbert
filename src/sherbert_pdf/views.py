"""
Views for the sherbert_pdf app.

``EditorView`` serves the Konva.js-based PDF annotation editor as a
self-contained standalone page. Access is resolved through the pluggable
access policy (``sherbert_pdf.access``); denial renders as a 404 so the
existence of documents is not leaked.
"""
from django.conf import settings
from django.http import Http404
from django.templatetags.static import static
from django.views.generic import TemplateView

from .access import check_pdf_access


def default_stamps():
    """Two built-in demo stamps shipped in the package. Used when the host
    project does not define ``SHERBERT_PDF_STAMPS``. URLs are resolved through
    ``static()`` so they honour the host's ``STATIC_URL``."""
    return [
        {'label': 'Approved', 'url': static('sherbert_pdf/stamps/approved.png')},
        {'label': 'Rejected', 'url': static('sherbert_pdf/stamps/rejected.png')},
    ]


class EditorView(TemplateView):
    """Standalone Konva.js PDF annotation editor page."""

    template_name = 'sherbert_pdf/editor.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        has_access, pdf_doc = check_pdf_access(
            self.request.user,
            pdf_document_id=kwargs['pk'],
        )
        if not has_access:
            raise Http404('PDF document not found')

        # The API router is mounted by the host project; default matches the
        # example project's mount point. Hosts can override via settings.
        api_base = getattr(settings, 'SHERBERT_PDF_API_BASE', '/api')

        # Configurable stamp palette: list of {'label', 'url'}. Falls back to
        # the two package demo stamps when the host has not configured any.
        stamps = getattr(settings, 'SHERBERT_PDF_STAMPS', None)
        if not stamps:
            stamps = default_stamps()

        context['pdf'] = pdf_doc
        context['api_base'] = api_base
        context['editor_config'] = {
            'pdfId': pdf_doc.id,
            'fileUrl': pdf_doc.file.url if pdf_doc.file else '',
            'apiBase': api_base,
            'userId': self.request.user.id,
            'stamps': stamps,
        }
        return context
