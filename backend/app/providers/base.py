from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, Optional, List, Tuple
from dataclasses import dataclass
from app.models import RemotePost


class Provider(ABC):
    """Abstract base class for all content providers."""

    @abstractmethod
    async def search(
        self,
        query: str,
        cursor: Optional[str] = None,
        limit: int = 100,
        sort: str = "latest",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Tuple[List[RemotePost], Optional[str]]:
        """
        Search posts matching query.

        Args:
            query: Tag query string
            cursor: Pagination cursor (provider-specific)
            limit: Maximum results per page

        Returns:
            Tuple of (posts, next_cursor)
        """
        pass

    @abstractmethod
    async def download_image(
        self,
        post: RemotePost,
        dest_path: str,
        progress: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """
        Download the image file to dest_path.

        Args:
            post: Remote post metadata
            dest_path: Destination file path
            progress: Optional callback receiving current-file transfer metrics
        """
        pass

    @abstractmethod
    async def get_post(self, remote_id: str) -> RemotePost:
        """
        Fetch metadata for a single post by ID.

        Args:
            remote_id: Provider-specific post ID

        Returns:
            RemotePost with full metadata
        """
        pass
