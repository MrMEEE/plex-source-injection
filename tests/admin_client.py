"""In-process routing between the two listeners, sharing a TestClient lifespan."""

from main import create_app as create_proxy_app


class SharedTestApp:
    def __init__(self, proxy):
        self.proxy = proxy
        self.state = proxy.state

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "lifespan" and (path.rstrip("/") == "/admin" or path.startswith("/admin/")):
            await self.state.admin_app(scope, receive, send)
        else:
            await self.proxy(scope, receive, send)


def create_app(**kwargs):
    return SharedTestApp(create_proxy_app(**kwargs))
