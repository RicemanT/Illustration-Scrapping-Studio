"""Read-only, SQL-backed local dataset queries. No sidecar or cursor writes."""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator, field_validator

from app.db import get_connection
from app.services.tags import normalize_tags


class LocalFilter(BaseModel):
    model_config = {"extra": "forbid"}
    required_tags: list[str] = Field(default_factory=list, max_length=32)
    excluded_tags: list[str] = Field(default_factory=list, max_length=32)
    tag_basis: Literal["effective", "provenance"] = "effective"
    provider: str | None = Field(default=None, max_length=64)
    rating: Literal["safe", "questionable", "explicit", "unknown"] | None = None
    min_width: int | None = Field(default=None, ge=1)
    max_width: int | None = Field(default=None, ge=1)
    min_height: int | None = Field(default=None, ge=1)
    max_height: int | None = Field(default=None, ge=1)
    review_status: Literal["pending", "accepted", "rejected", "archived"] | None = None
    favorite: bool | None = None
    image_ids: list[int] | None = Field(default=None, max_length=500)

    @field_validator("required_tags", "excluded_tags")
    @classmethod
    def tags(cls, value):
        result = normalize_tags(value)
        if any(len(tag) > 256 for tag in result):
            raise ValueError("Tags must be at most 256 characters")
        return result

    @model_validator(mode="after")
    def ranges(self):
        for axis in ("width", "height"):
            low, high = getattr(self, f"min_{axis}"), getattr(self, f"max_{axis}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"Minimum {axis} exceeds maximum")
        return self


class LocalQuery(BaseModel):
    model_config = {"extra": "forbid"}
    filters: LocalFilter = Field(default_factory=LocalFilter)
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    sort: Literal["newest", "oldest", "width", "height"] = "newest"


# Effective membership mirrors TagService: trusted source tags, canonical
# folder trigger, category policy and explicit overrides. A live SQL view of
# these facts avoids a second index that could go stale on edits/undo/merge.
def query_sql(folder_id: int, filters: LocalFilter):
    cte = """WITH scope AS (
      SELECT i.*, COALESCE(c.ground_truth_categories,
        (SELECT value FROM app_setting WHERE key='ground_truth_categories'),
        '["artist","character","copyright","species","general"]') AS categories,
        c.name AS folder_name, c.artist_tag_template
      FROM image i JOIN collection_image ci ON ci.image_id=i.id AND ci.collection_id=i.folder_id
      JOIN collection c ON c.id=i.folder_id WHERE i.folder_id=?
    ), latest AS (
      SELECT s.* FROM image_source s JOIN scope i ON i.id=s.image_id
      WHERE NOT EXISTS (SELECT 1 FROM image_source n WHERE n.image_id=s.image_id
        AND n.provider=s.provider AND n.remote_id=s.remote_id AND n.version>s.version)
    ), source_tags AS (
      SELECT t.image_id, t.category, tag_normalize(t.tag) AS tag
      FROM image_tag t JOIN image_source s ON s.id=t.source_id JOIN scope i ON i.id=t.image_id
      WHERE s.provider IN ('danbooru','gelbooru','e621') AND t.category<>'artist'
        AND t.category IN (SELECT value FROM json_each(i.categories))
      UNION ALL
      SELECT i.id, 'artist', tag_normalize(replace(i.artist_tag_template,'{artist}',tag_key(i.folder_name)))
      FROM scope i WHERE i.artist_tag_template IS NOT NULL AND i.artist_tag_template<>''
        AND 'artist' IN (SELECT value FROM json_each(i.categories))
    ), effective_raw AS (
      SELECT t.image_id,t.category,t.tag FROM source_tags t WHERE t.tag<>'' AND NOT EXISTS (
        SELECT 1 FROM image_tag_override o JOIN scope i ON i.id=o.image_id
        WHERE o.image_id=t.image_id AND tag_key(o.tag)=tag_key(t.tag)
          AND (o.action='remove' OR (o.action='add' AND o.category IN (SELECT value FROM json_each(i.categories)))))
      UNION ALL
      SELECT o.image_id,o.category,tag_normalize(o.tag) FROM image_tag_override o JOIN scope i ON i.id=o.image_id
      WHERE o.action='add' AND o.category IN (SELECT value FROM json_each(i.categories))
    ), effective AS (
      SELECT image_id, category, tag FROM (
        SELECT image_id,category,tag, row_number() OVER (PARTITION BY image_id,tag_key(tag)
          ORDER BY CASE category WHEN 'artist' THEN 0 WHEN 'character' THEN 1 WHEN 'copyright' THEN 2
          WHEN 'species' THEN 3 WHEN 'general' THEN 4 WHEN 'meta' THEN 5 ELSE 6 END) AS position
        FROM effective_raw) WHERE position=1
    ), provenance AS (
      SELECT t.image_id,t.category,tag_normalize(t.tag) AS tag FROM image_tag t JOIN latest s ON s.id=t.source_id
    ) """
    clauses, params = ["1=1"], [folder_id]
    # A cheap indexed superset narrows selective AND queries before policy,
    # provenance-version and override checks. INTERSECT is evaluated in SQLite,
    # so common tags do not force Python UDF work on every folder image.
    candidates = []
    for tag in filters.required_tags:
        key = tag.casefold()
        candidate = "SELECT image_id FROM image_tag WHERE tag_key(tag)=?"
        params.append(key)
        if filters.tag_basis == 'effective':
            candidate += """ UNION SELECT image_id FROM image_tag_override WHERE tag_key(tag)=? AND action='add'
              UNION SELECT ix.id FROM collection c JOIN image ix ON ix.folder_id=c.id
              WHERE c.id=? AND tag_key(replace(c.artist_tag_template,'{artist}',tag_key(c.name)))=?"""
            params.extend([key,folder_id,key])
        candidates.append(f"SELECT image_id FROM ({candidate})")
    if candidates:
        clauses.append('i.id IN (' + ' INTERSECT '.join(candidates) + ')')
    for field, op in (("min_width", ">="), ("max_width", "<="), ("min_height", ">="), ("max_height", "<=")):
        value = getattr(filters, field)
        if value is not None:
            clauses.append(f"i.{field.split('_')[1]} {op} ?")
            params.append(value)
    for field in ("review_status", "favorite"):
        value = getattr(filters, field)
        if value is not None:
            clauses.append(f"i.{field}=?")
            params.append(value)
    if filters.image_ids is not None:
        clauses.append("i.id IN (SELECT value FROM json_each(?))")
        import json
        params.append(json.dumps(filters.image_ids))
    source_conditions = []
    if filters.provider:
        source_conditions.append("s.provider=?")
        params.append(filters.provider)
    rating_expr = """CASE lower(COALESCE(json_extract(CASE WHEN json_valid(s.metadata) THEN s.metadata ELSE '{}' END,'$.rating'),''))
        WHEN 's' THEN 'safe' WHEN 'g' THEN 'safe' WHEN 'general' THEN 'safe' WHEN 'safe' THEN 'safe'
        WHEN 'q' THEN 'questionable' WHEN 'questionable' THEN 'questionable'
        WHEN 'e' THEN 'explicit' WHEN 'explicit' THEN 'explicit' ELSE 'unknown' END"""
    if filters.rating:
        source_conditions.append(f"({rating_expr})=?")
        params.append(filters.rating)
    latest_condition = "NOT EXISTS (SELECT 1 FROM image_source n WHERE n.image_id=s.image_id AND n.provider=s.provider AND n.remote_id=s.remote_id AND n.version>s.version)"
    if source_conditions:
        match = "EXISTS (SELECT 1 FROM image_source s WHERE s.image_id=i.id AND " + latest_condition + " AND " + " AND ".join(source_conditions) + ")"
        if filters.rating == "unknown" and not filters.provider:
            match = f"({match} OR NOT EXISTS (SELECT 1 FROM image_source s WHERE s.image_id=i.id))"
        clauses.append(match)
    basis = filters.tag_basis
    for tags, exclude in ((filters.required_tags, False), (filters.excluded_tags, True)):
        for tag in tags:
            key = tag.casefold()
            if basis == 'provenance':
                match = f"""EXISTS (SELECT 1 FROM image_tag t JOIN image_source s ON s.id=t.source_id
                    WHERE t.image_id=i.id AND tag_key(t.tag)=? AND {latest_condition})"""
                params.append(key)
            else:
                match = """(EXISTS (SELECT 1 FROM image_tag_override o WHERE o.image_id=i.id
                    AND tag_key(o.tag)=? AND o.action='add' AND o.category IN (SELECT value FROM json_each(i.categories)))
                  OR (NOT EXISTS (SELECT 1 FROM image_tag_override o WHERE o.image_id=i.id
                    AND tag_key(o.tag)=? AND o.action='remove') AND (
                    (i.artist_tag_template IS NOT NULL AND tag_key(replace(i.artist_tag_template,'{artist}',tag_key(i.folder_name)))=?
                     AND 'artist' IN (SELECT value FROM json_each(i.categories)))
                    OR EXISTS (SELECT 1 FROM image_tag t JOIN image_source s ON s.id=t.source_id
                      WHERE t.image_id=i.id AND tag_key(t.tag)=? AND t.category<>'artist'
                      AND t.category IN (SELECT value FROM json_each(i.categories))
                      AND s.provider IN ('danbooru','gelbooru','e621')))))"""
                params.extend([key]*4)
            clauses.append(('NOT ' if exclude else '') + match)
    return cte, " AND ".join(clauses), params


def list_images(folder_id: int, query: LocalQuery) -> dict:
    cte, where, params = query_sql(folder_id, query.filters)
    conn = get_connection()
    try:
        if not conn.execute("SELECT 1 FROM collection WHERE id=?", (folder_id,)).fetchone():
            raise LookupError("Folder not found")
        conn.execute("BEGIN")
        total = conn.execute(cte + "SELECT count(*) FROM scope i WHERE " + where, params).fetchone()[0]
        order = {"newest": "i.added_at DESC,i.id DESC", "oldest": "i.added_at,i.id", "width": "i.width DESC,i.id DESC", "height": "i.height DESC,i.id DESC"}[query.sort]
        rows = conn.execute(cte + f"SELECT i.* FROM scope i WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, query.limit, query.offset]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("categories", "folder_name", "artist_tag_template"):
                item.pop(key, None)
            item["sidecar_path"] = Path(item["path"]).with_suffix(".txt").as_posix()
            items.append(item)
        return {"items": items, "images": items, "total": total, "next_cursor": str(query.offset + len(items)) if query.offset + len(items) < total else None,
                "limit": query.limit, "offset": query.offset, "filters": query.filters.model_dump()}
    finally:
        conn.close()


def explore_tags(folder_id: int, query: LocalQuery, search: str = "", category: str | None = None):
    cte, where, params = query_sql(folder_id, query.filters)
    # Restrict tag construction to already matched IDs; gallery predicates use
    # indexed per-image lookups and never materialize the entire tag relation.
    cte = cte.replace('), latest AS (', f'), matches AS MATERIALIZED (SELECT i.* FROM scope i WHERE {where}), latest AS (')
    cte = cte.replace('JOIN scope i', 'JOIN matches i').replace('FROM scope i WHERE i.artist_tag_template', 'FROM matches i WHERE i.artist_tag_template')
    sql = cte + f""", counts AS (
      SELECT t.category, min(t.tag) AS tag, count(DISTINCT t.image_id) AS count
      FROM {query.filters.tag_basis} t JOIN matches m ON m.id=t.image_id
      WHERE instr(tag_key(t.tag),?)>0 AND (? IS NULL OR t.category=?)
      GROUP BY t.category,tag_key(t.tag)) """
    params += [search.casefold(), category, category]
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        total = conn.execute(sql + "SELECT count(*) FROM counts", params).fetchone()[0]
        items = [dict(r) for r in conn.execute(sql + "SELECT * FROM counts ORDER BY count DESC,category,tag LIMIT ? OFFSET ?", [*params, query.limit, query.offset])]
        return {"items": items, "total": total, "next_cursor": str(query.offset + len(items)) if query.offset + len(items) < total else None}
    finally:
        conn.close()
