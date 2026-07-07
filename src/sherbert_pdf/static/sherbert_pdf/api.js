/* API client for the sherbert_pdf Django Ninja router.
 * Payload shapes intentionally match CoreCRM quick_edit4.html byte-for-byte
 * (see saveDrawingAnnotation / saveTextAnnotation / updateAnnotationPosition). */

let apiBase = '/api';

export function initApi(base) {
  apiBase = base.replace(/\/$/, '');
}

export function getCsrfToken() {
  const input = document.querySelector('input[name=csrfmiddlewaretoken]');
  if (input) return input.value;
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? match[1] : '';
}

async function request(method, path, body) {
  const response = await fetch(`${apiBase}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrfToken(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(`${method} ${path} failed: ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

/** GET /pdf-documents/{id}/annotations → AnnotationOut[] */
export function listAnnotations(pdfId) {
  return request('GET', `/pdf-documents/${pdfId}/annotations`);
}

/** POST /annotations with the AnnotationCreate schema; returns AnnotationOut. */
export function createAnnotation(pdfDocumentId, pageNumber, annotationType, annotationData) {
  return request('POST', '/annotations', {
    pdf_document_id: pdfDocumentId,
    annotation: {
      page_number: pageNumber, // 0-indexed for the backend / pymupdf
      annotation_type: annotationType,
      annotation_data: annotationData,
    },
  });
}

/** PUT /annotations with the AnnotationUpdate schema (full annotation_data). */
export function updateAnnotation(annotationId, annotationData) {
  return request('PUT', '/annotations', {
    annotation_id: annotationId,
    annotation_data: annotationData,
  });
}

/** DELETE /annotations with the AnnotationDelete schema. */
export function deleteAnnotation(annotationId) {
  return request('DELETE', '/annotations', {
    annotation_id: annotationId,
  });
}
