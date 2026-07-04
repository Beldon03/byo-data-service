from pydantic import BaseModel


class ColumnSchema(BaseModel):
    name: str
    type: str


class DatasetCreated(BaseModel):
    name: str
    table: str
    columns: list[ColumnSchema]
    row_count: int


class DatasetSummary(BaseModel):
    name: str
    row_count: int
    created_at: str


class DatasetSchema(BaseModel):
    columns: list[ColumnSchema]
