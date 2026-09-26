from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any
from datetime import datetime


# Folder models. Collection* aliases below keep older service imports working.
class FolderBase(BaseModel):
    name: str
    group_id: Optional[int] = Field(default=None, gt=0)
    type: str = Field(default="artist", description="artist, character, or tag")
    query: str
    artist_tag_template: Optional[str] = Field(
        default="Drawn by {artist}",
        description="Optional ground-truth format applied separately to every imported artist credit",
        max_length=200,
    )
    caption_template: Dict[str, Any] = Field(default_factory=lambda: {
        "version": 1,
        "trigger": "",
        "trigger_position": "prefix",
        "include_categories": ["character", "copyright", "species", "general"],
        "exclude_categories": ["artist", "meta"],
        "exclude_tags": ["highres", "absurdres", "commentary"],
        "tag_order": ["trigger", "character", "copyright", "species", "general"],
        "separator": ", ",
        "underscore_handling": "replace_with_space",
        "case": "lowercase",
        "max_tags": None,
        "min_tag_score": 0,
        "custom_replacements": {}
    })
    filters: Dict[str, Any] = Field(default_factory=lambda: {
        "min_width": 512,
        "min_height": 512,
        "max_aspect_ratio": 3.0,
        "min_aspect_ratio": 0.33,
        "allowed_ratings": ["safe", "questionable"],
        "blocked_tags": [],
        "allowed_formats": ["jpg", "webp"],
        "blocked_formats": ["gif", "webm", "mp4"],
        "skip_ugoira": True
    })

    @field_validator("artist_tag_template")
    @classmethod
    def validate_artist_tag_template(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if "{artist}" not in value:
            raise ValueError("artist tag format must contain {artist}")
        if any(character in value for character in (",", "\n", "\r")):
            raise ValueError("artist tag format cannot contain commas or line breaks")
        return value


class FolderCreate(FolderBase):
    sources: List[str] = Field(default_factory=lambda: ["danbooru"])
    source_queries: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_collection(self):
        from app.services.queries import validate_collection_query
        if not self.name.strip() or len(self.name) > 200 or any(ord(c) < 32 for c in self.name):
            raise ValueError('Collection name must be 1-200 characters without control characters')
        self.name = self.name.strip()
        validate_collection_query(self.query, self.type)
        self.sources = list(dict.fromkeys(self.sources))
        if set(self.source_queries) - set(self.sources):
            raise ValueError('Source queries must belong to selected sources')
        for provider in self.sources:
            validate_collection_query(self.source_queries.get(provider) or self.query, self.type, provider)
        if self.type != 'artist':
            self.artist_tag_template = None
        return self



class Folder(FolderBase):
    id: int
    slug: str
    created_at: str
    updated_at: str
    last_sync_at: Optional[str] = None
    enabled: bool = True

    class Config:
        from_attributes = True


class FolderWithStats(Folder):
    image_count: int = 0
    sources: List[Dict[str, Any]] = []


# Non-destructive compatibility for legacy module and service names.
CollectionBase = FolderBase
CollectionCreate = FolderCreate
Collection = Folder
CollectionWithStats = FolderWithStats


# Image models
class ImageBase(BaseModel):
    sha256: str
    md5: Optional[str] = None
    width: int
    height: int
    format: str
    file_size: int


class Image(ImageBase):
    id: int
    phash: Optional[int] = None
    dhash: Optional[int] = None
    gradient_hash: Optional[str] = None
    colorhash: Optional[str] = None
    path: str
    thumb_path: Optional[str] = None
    added_at: str
    review_status: str = "pending"
    favorite: bool = False
    notes: Optional[str] = None
    derived_from_preview: bool = False
    original_media_format: Optional[str] = None
    original_media_url: Optional[str] = None
    derived_media_source: Optional[str] = None

    class Config:
        from_attributes = True


class ImageWithMetadata(Image):
    sources: List[Dict[str, Any]] = []
    tags: Dict[str, List[str]] = {}
    folders: List[str] = []
    # Legacy response field retained for older clients.
    collections: List[str] = []


class ImageReviewUpdate(BaseModel):
    review_status: Optional[str] = None
    favorite: Optional[bool] = None
    notes: Optional[str] = None


class FolderImageBulkAction(BaseModel):
    image_ids: List[int] = Field(default_factory=list)
    action: str = "remove"


CollectionImageBulkAction = FolderImageBulkAction


class DedupScanRequest(BaseModel):
    folder_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("folder_id", "collection_id"))
    profile: str = "balanced"


class DuplicateResolutionRequest(BaseModel):
    action: str


class GroundTruthTagsReplace(BaseModel):
    tags: List[str] = Field(default_factory=list)


class GroundTruthTagsBulkAction(BaseModel):
    image_ids: List[int] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    action: str


class GroundTruthTagBatchReplace(BaseModel):
    image_ids: List[int] = Field(default_factory=list)
    old_tag: str
    new_tag: str


class TagCategoryPolicyUpdate(BaseModel):
    categories: Optional[List[str]] = None


class ParallelismSettings(BaseModel):
    workers: int = Field(default=2, ge=1, strict=True)


class SyncScheduleUpdate(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=1440, ge=5, le=10080)
    limit_per_source: int = Field(default=20, ge=1, le=320)
    sort: str = Field(default="latest", pattern="^latest$")


class ImportPreviewRequest(BaseModel):
    folder_id: int = Field(validation_alias=AliasChoices("folder_id", "collection_id"))
    provider: str
    query: str
    cursor: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=320)
    sort: str = Field(default="latest", pattern="^(latest|oldest)$")
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class ImportBatchCreate(BaseModel):
    folder_id: int = Field(validation_alias=AliasChoices("folder_id", "collection_id"))
    provider: str
    remote_ids: List[str] = Field(min_length=1)


class DatasetExportCreate(BaseModel):
    folder_id: int = Field(validation_alias=AliasChoices("folder_id", "collection_id"))
    mode: str = Field(default="copy", pattern="^(copy|hardlink)$")


# Source models
class RemotePost(BaseModel):
    provider: str
    remote_id: str
    remote_url: str
    image_url: str
    preview_url: Optional[str] = None
    width: int
    height: int
    format: str
    md5: Optional[str] = None
    tags: Dict[str, List[str]]
    rating: Optional[str] = None
    score: Optional[int] = None
    source: Optional[str] = None
    parent_id: Optional[str] = None
    created_at: str
    raw_metadata: Dict[str, Any]


class ImageSource(BaseModel):
    id: int
    image_id: int
    provider: str
    remote_id: str
    remote_url: Optional[str]
    source_url: Optional[str] = None
    fetched_at: str
    metadata: Dict[str, Any]
    version: int = 1
    metadata_version: int = 1
    is_primary: bool = False


# Tag models
class Tag(BaseModel):
    category: str
    tag: str
    count: int


# Sync models
class SyncStatus(BaseModel):
    folder_id: int = Field(validation_alias=AliasChoices("folder_id", "collection_id"))
    folder_name: str = Field(validation_alias=AliasChoices("folder_name", "collection_name"))
    status: str  # idle, syncing, error
    last_sync_at: Optional[str]
    images_added: int = 0
    error_message: Optional[str] = None


class SyncResult(BaseModel):
    folder_id: int = Field(validation_alias=AliasChoices("folder_id", "collection_id"))
    provider: str
    new_images: int
    skipped: int
    errors: int
    duration_seconds: float


class ProcessingSettings(BaseModel):
    enabled: bool = True
    max_dimension: Optional[int] = Field(default=2000, ge=1)
    resize_filter: str = 'area'
    output_format: str = 'auto'
    webp_lossless: bool = True
    webp_quality: int = Field(default=80, ge=0, le=100)
    webp_method: int = Field(default=4, ge=0, le=6)
    jpeg_quality: int = Field(default=100, ge=1, le=100)

    @field_validator('resize_filter')
    @classmethod
    def resize_choice(cls, value):
        if value not in {'area', 'lanczos', 'cubic', 'linear'}: raise ValueError('Unsupported resize filter')
        return value

    @field_validator('output_format')
    @classmethod
    def output_choice(cls, value):
        if value not in {'auto', 'webp', 'jpg', 'png'}: raise ValueError('Choose auto, webp, jpg or png')
        return value
