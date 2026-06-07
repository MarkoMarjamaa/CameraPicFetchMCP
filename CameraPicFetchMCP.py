import os
import base64
import io
import threading
import contextlib
from typing import Dict, Optional, Literal, TypedDict, Any

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib.parse import urlparse, urlunparse

from PIL import Image as PIL_Image

from requests_testadapter import Resp

from mcp.server.fastmcp import FastMCP, Image as MCP_Image

from mcp.types import ImageContent

# -----------------------------
# Requests adapter for file://
# -----------------------------
class LocalFileAdapter(requests.adapters.HTTPAdapter):
	def build_response_from_file(self, request):
		file_path = request.url[7:]  # strip "file://"
		if not os.path.isfile(file_path):
			raise FileNotFoundError(f"File not found: {file_path}")

		with open(file_path, "rb") as file:
			buff = bytearray(os.path.getsize(file_path))
			file.readinto(buff)
			resp = Resp(buff)
			r = self.build_response(request, resp)
			return r

	def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
		return self.build_response_from_file(request)


# ---- Configuration ----
AuthType = Literal["none", "basic", "digest"]


class CameraConfig(TypedDict):
	url: str
	auth_type: AuthType
	username: Optional[str]
	password: Optional[str]
	verify_tls: bool


SECURITY_CAMERAS: dict[str, CameraConfig] = {
	"ImageFile": {
		"url": "file:///home/testpicture.jpg",
		"auth_type": "none",
		"username": None,
		"password": None,
		"verify_tls": True,
	},
	"ExampleCamera": {
		"url": "http://server/directory/picture",
		"auth_type": "digest",   # try "basic" first; many cameras need "digest"
		"username": "admin",
		"password": "admin",
		"verify_tls": True,
	},
	"BlackbirdNest": {
		"url": "http://192.168.1.1/snap.jpeg",
		"auth_type": "none",   # try "basic" first; many cameras need "digest"
		"username": None,
		"password": None,
		"verify_tls": True,
	},
}

def _strip_userinfo(url: str) -> str:
	"""Remove user:pass@ from url if present, to avoid leaking creds in logs."""
	p = urlparse(url)
	if p.username or p.password:
		netloc = p.hostname or ""
		if p.port:
			netloc += f":{p.port}"
		return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
	return url


def fetch_image_from_camera(cam: CameraConfig) -> PIL_Image.Image:
	session = requests.session()
	session.mount("file://", LocalFileAdapter())

	url = cam["url"]

	try:
		if url.startswith("file://"):
			resp = session.get(url, stream=True)
		else:
			safe_url = _strip_userinfo(url)
			auth = None
			if cam.get("auth_type") == "basic":
				auth = HTTPBasicAuth(cam.get("username") or "", cam.get("password") or "")
			elif cam.get("auth_type") == "digest":
				auth = HTTPDigestAuth(cam.get("username") or "", cam.get("password") or "")

			headers = {
				"User-Agent": "camera-vision-mcp/1.0",
				"Accept": "image/*,*/*;q=0.8",
			}

			resp = session.get(
				safe_url,
				stream=True,
				timeout=10,
				auth=auth,
				headers=headers,
				verify=cam.get("verify_tls", True),
				allow_redirects=True,
			)

			resp.raise_for_status()

			ctype = (resp.headers.get("Content-Type") or "").lower()
			if "image" not in ctype:
				raise ValueError(
					f"Camera did not return an image. Content-Type={ctype} Status={resp.status_code}"
				)

		return PIL_Image.open(resp.raw).convert("RGB")

	except requests.HTTPError as e:
		www = ""
		try:
			www = resp.headers.get("WWW-Authenticate", "")
		except Exception:
			pass
		raise PermissionError(f"HTTP error fetching camera image: {e}. WWW-Authenticate={www}") from e
	except Exception as e:
		raise RuntimeError(f"Failed to fetch/parse camera image: {e}") from e


# -----------------------------
# MCP server setup
# -----------------------------
mcp = FastMCP(
	name="Camera Picture Service",
)

@mcp.resource("cameras://list")
def list_cameras() -> list[str]:
	"""List available camera names."""
	return sorted(SECURITY_CAMERAS.keys())


@mcp.resource("service://health")
def health() -> Dict[str, Any]:
	"""Service health and configuration."""
	return {
		"status": "ok",
		"cameras": sorted(SECURITY_CAMERAS.keys()),
	}


@mcp.tool()
def get_camera_picture(camera_name: str) -> MCP_Image:
	"""
	Capture one still image from a named camera and return it as an MCP image content block.

	Args:
		camera_name: One of the configured camera names (FrontDoor, BackYard, InnerYard, BlackbirdNest).
	"""
	camera_name = (camera_name or "").strip()

	if not camera_name:
		raise ValueError("camera_name is required")
	if camera_name not in SECURITY_CAMERAS:
		raise KeyError(
			f"Unknown camera_name '{camera_name}'. Allowed: {sorted(SECURITY_CAMERAS.keys())}"
		)

	image = fetch_image_from_camera(SECURITY_CAMERAS[camera_name])
	buf = io.BytesIO()
	image.save(buf, format="JPEG")

	return MCP_Image(data = buf.getvalue(), format="jpeg")

	
if __name__ == "__main__":
	mcp.run(transport="stdio")
	
