"""
Pydantic schemas for Django Ninja API endpoints.
"""
from pydantic import BaseModel, Field
from typing import List, Union, Literal, Optional


# PDF Document Schemas
class PDFDocumentOut(BaseModel):
    id: int
    title: str
    file_url: str


# Annotation Type Schemas - Proper MuPDF-compatible structures
class RGBColor(BaseModel):
    """RGB color as array of 3 floats (0.0-1.0)"""
    stroke: List[float] = Field(..., min_length=3, max_length=3)


class BorderStyle(BaseModel):
    """Border style for annotations"""
    width: float = Field(..., ge=0)


class ErasureCircle(BaseModel):
    """A single eraser circle punched into a highlight mask (PDF coordinate space)"""
    cx: float
    cy: float
    r: float


class PenAnnotationData(BaseModel):
    """Pen/Highlighter annotation data"""
    vertices: List[List[List[float]]] = Field(..., description="Array of points: [[[x1,y1], [x2,y2], ...]]")
    colors: RGBColor
    border: BorderStyle
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    erasures: Optional[List[ErasureCircle]] = Field(default=None)


class TextAnnotationData(BaseModel):
    """Text annotation data"""
    rect: List[float] = Field(..., min_length=4, max_length=4, description="[x1, y1, x2, y2]")
    content: str
    colors: RGBColor
    fontSize: int = Field(default=16, ge=1)
    fontFamily: str = Field(default='Arial, sans-serif')
    fontStyle: str = Field(default='normal')


class StampAnnotationData(BaseModel):
    """Stamp annotation data"""
    type: Literal["stamp"] = "stamp"
    x: float
    y: float
    width: float = Field(..., gt=0)
    height: float = Field(..., gt=0)
    imageUrl: str


# Union type for all annotation data types
AnnotationDataUnion = Union[PenAnnotationData, TextAnnotationData, StampAnnotationData]


# PDF Annotation Request/Response Schemas
class AnnotationData(BaseModel):
    page_number: int
    annotation_type: Literal["pen", "highlighter", "text", "stamp", "cloud"]
    annotation_data: AnnotationDataUnion


class AnnotationCreate(BaseModel):
    pdf_document_id: int
    annotation: AnnotationData


class AnnotationUpdate(BaseModel):
    annotation_id: int
    annotation_data: AnnotationDataUnion


class AnnotationDelete(BaseModel):
    annotation_id: int


class AnnotationOut(BaseModel):
    id: int
    page_number: int
    annotation_type: str
    annotation_data: AnnotationDataUnion
    user_id: int
    is_owner: bool
    user_name: str
