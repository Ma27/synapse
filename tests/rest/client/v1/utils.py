# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017 Vector Creations Ltd
# Copyright 2018-2019 New Vector Ltd
# Copyright 2019-2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import re
import time
import urllib.parse
from typing import Any, Dict, Optional

from mock import patch

import attr

from twisted.web.resource import Resource
from twisted.web.server import Site

from synapse.api.constants import Membership
from synapse.types import JsonDict

from tests.server import FakeSite, make_request
from tests.test_utils import FakeResponse


@attr.s
class RestHelper:
    """Contains extra helper functions to quickly and clearly perform a given
    REST action, which isn't the focus of the test.
    """

    hs = attr.ib()
    site = attr.ib(type=Site)
    auth_user_id = attr.ib()

    def create_room_as(
        self,
        room_creator: str = None,
        is_public: bool = True,
        room_version: str = None,
        tok: str = None,
        expect_code: int = 200,
    ) -> str:
        """
        Create a room.

        Args:
            room_creator: The user ID to create the room with.
            is_public: If True, the `visibility` parameter will be set to the
                default (public). Otherwise, the `visibility` parameter will be set
                to "private".
            room_version: The room version to create the room as. Defaults to Synapse's
                default room version.
            tok: The access token to use in the request.
            expect_code: The expected HTTP response code.

        Returns:
            The ID of the newly created room.
        """
        temp_id = self.auth_user_id
        self.auth_user_id = room_creator
        path = "/_matrix/client/r0/createRoom"
        content = {}
        if not is_public:
            content["visibility"] = "private"
        if room_version:
            content["room_version"] = room_version
        if tok:
            path = path + "?access_token=%s" % tok

        _, channel = make_request(
            self.hs.get_reactor(),
            self.site,
            "POST",
            path,
            json.dumps(content).encode("utf8"),
        )

        assert channel.result["code"] == b"%d" % expect_code, channel.result
        self.auth_user_id = temp_id

        if expect_code == 200:
            return channel.json_body["room_id"]

    def invite(self, room=None, src=None, targ=None, expect_code=200, tok=None):
        self.change_membership(
            room=room,
            src=src,
            targ=targ,
            tok=tok,
            membership=Membership.INVITE,
            expect_code=expect_code,
        )

    def join(self, room=None, user=None, expect_code=200, tok=None):
        self.change_membership(
            room=room,
            src=user,
            targ=user,
            tok=tok,
            membership=Membership.JOIN,
            expect_code=expect_code,
        )

    def leave(self, room=None, user=None, expect_code=200, tok=None):
        self.change_membership(
            room=room,
            src=user,
            targ=user,
            tok=tok,
            membership=Membership.LEAVE,
            expect_code=expect_code,
        )

    def change_membership(
        self,
        room: str,
        src: str,
        targ: str,
        membership: str,
        extra_data: dict = {},
        tok: Optional[str] = None,
        expect_code: int = 200,
    ) -> None:
        """
        Send a membership state event into a room.

        Args:
            room: The ID of the room to send to
            src: The mxid of the event sender
            targ: The mxid of the event's target. The state key
            membership: The type of membership event
            extra_data: Extra information to include in the content of the event
            tok: The user access token to use
            expect_code: The expected HTTP response code
        """
        temp_id = self.auth_user_id
        self.auth_user_id = src

        path = "/_matrix/client/r0/rooms/%s/state/m.room.member/%s" % (room, targ)
        if tok:
            path = path + "?access_token=%s" % tok

        data = {"membership": membership}
        data.update(extra_data)

        _, channel = make_request(
            self.hs.get_reactor(),
            self.site,
            "PUT",
            path,
            json.dumps(data).encode("utf8"),
        )

        assert int(channel.result["code"]) == expect_code, (
            "Expected: %d, got: %d, resp: %r"
            % (expect_code, int(channel.result["code"]), channel.result["body"])
        )

        self.auth_user_id = temp_id

    def send(self, room_id, body=None, txn_id=None, tok=None, expect_code=200):
        if body is None:
            body = "body_text_here"

        content = {"msgtype": "m.text", "body": body}

        return self.send_event(
            room_id, "m.room.message", content, txn_id, tok, expect_code
        )

    def send_event(
        self, room_id, type, content={}, txn_id=None, tok=None, expect_code=200
    ):
        if txn_id is None:
            txn_id = "m%s" % (str(time.time()))

        path = "/_matrix/client/r0/rooms/%s/send/%s/%s" % (room_id, type, txn_id)
        if tok:
            path = path + "?access_token=%s" % tok

        _, channel = make_request(
            self.hs.get_reactor(),
            self.site,
            "PUT",
            path,
            json.dumps(content).encode("utf8"),
        )

        assert int(channel.result["code"]) == expect_code, (
            "Expected: %d, got: %d, resp: %r"
            % (expect_code, int(channel.result["code"]), channel.result["body"])
        )

        return channel.json_body

    def _read_write_state(
        self,
        room_id: str,
        event_type: str,
        body: Optional[Dict[str, Any]],
        tok: str,
        expect_code: int = 200,
        state_key: str = "",
        method: str = "GET",
    ) -> Dict:
        """Read or write some state from a given room

        Args:
            room_id:
            event_type: The type of state event
            body: Body that is sent when making the request. The content of the state event.
                If None, the request to the server will have an empty body
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:
            method: "GET" or "PUT" for reading or writing state, respectively

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        path = "/_matrix/client/r0/rooms/%s/state/%s/%s" % (
            room_id,
            event_type,
            state_key,
        )
        if tok:
            path = path + "?access_token=%s" % tok

        # Set request body if provided
        content = b""
        if body is not None:
            content = json.dumps(body).encode("utf8")

        _, channel = make_request(
            self.hs.get_reactor(), self.site, method, path, content
        )

        assert int(channel.result["code"]) == expect_code, (
            "Expected: %d, got: %d, resp: %r"
            % (expect_code, int(channel.result["code"]), channel.result["body"])
        )

        return channel.json_body

    def get_state(
        self,
        room_id: str,
        event_type: str,
        tok: str,
        expect_code: int = 200,
        state_key: str = "",
    ):
        """Gets some state from a room

        Args:
            room_id:
            event_type: The type of state event
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        return self._read_write_state(
            room_id, event_type, None, tok, expect_code, state_key, method="GET"
        )

    def send_state(
        self,
        room_id: str,
        event_type: str,
        body: Dict[str, Any],
        tok: str,
        expect_code: int = 200,
        state_key: str = "",
    ):
        """Set some state in a room

        Args:
            room_id:
            event_type: The type of state event
            body: Body that is sent when making the request. The content of the state event.
            tok: The access token to use
            expect_code: The HTTP code to expect in the response
            state_key:

        Returns:
            The response body from the server

        Raises:
            AssertionError: if expect_code doesn't match the HTTP code we received
        """
        return self._read_write_state(
            room_id, event_type, body, tok, expect_code, state_key, method="PUT"
        )

    def upload_media(
        self,
        resource: Resource,
        image_data: bytes,
        tok: str,
        filename: str = "test.png",
        expect_code: int = 200,
    ) -> dict:
        """Upload a piece of test media to the media repo
        Args:
            resource: The resource that will handle the upload request
            image_data: The image data to upload
            tok: The user token to use during the upload
            filename: The filename of the media to be uploaded
            expect_code: The return code to expect from attempting to upload the media
        """
        image_length = len(image_data)
        path = "/_matrix/media/r0/upload?filename=%s" % (filename,)
        _, channel = make_request(
            self.hs.get_reactor(),
            FakeSite(resource),
            "POST",
            path,
            content=image_data,
            access_token=tok,
            custom_headers=[(b"Content-Length", str(image_length))],
        )

        assert channel.code == expect_code, "Expected: %d, got: %d, resp: %r" % (
            expect_code,
            int(channel.result["code"]),
            channel.result["body"],
        )

        return channel.json_body

    def login_via_oidc(self, remote_user_id: str) -> JsonDict:
        """Log in (as a new user) via OIDC

        Returns the result of the final token login.

        Requires that "oidc_config" in the homeserver config be set appropriately
        (TEST_OIDC_CONFIG is a suitable example) - and by implication, needs a
        "public_base_url".

        Also requires the login servlet and the OIDC callback resource to be mounted at
        the normal places.
        """
        client_redirect_url = "https://x"

        # first hit the redirect url (which will issue a cookie and state)
        _, channel = make_request(
            self.hs.get_reactor(),
            self.site,
            "GET",
            "/login/sso/redirect?redirectUrl=" + client_redirect_url,
        )
        # that will redirect to the OIDC IdP, but we skip that and go straight
        # back to synapse's OIDC callback resource. However, we do need the "state"
        # param that synapse passes to the IdP via query params, and the cookie that
        # synapse passes to the client.
        assert channel.code == 302
        oauth_uri = channel.headers.getRawHeaders("Location")[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(oauth_uri).query)
        redirect_uri = "%s?%s" % (
            urllib.parse.urlparse(params["redirect_uri"][0]).path,
            urllib.parse.urlencode({"state": params["state"][0], "code": "TEST_CODE"}),
        )
        cookies = {}
        for h in channel.headers.getRawHeaders("Set-Cookie"):
            parts = h.split(";")
            k, v = parts[0].split("=", maxsplit=1)
            cookies[k] = v

        # before we hit the callback uri, stub out some methods in the http client so
        # that we don't have to handle full HTTPS requests.

        # (expected url, json response) pairs, in the order we expect them.
        expected_requests = [
            # first we get a hit to the token endpoint, which we tell to return
            # a dummy OIDC access token
            ("https://issuer.test/token", {"access_token": "TEST"}),
            # and then one to the user_info endpoint, which returns our remote user id.
            ("https://issuer.test/userinfo", {"sub": remote_user_id}),
        ]

        async def mock_req(method: str, uri: str, data=None, headers=None):
            (expected_uri, resp_obj) = expected_requests.pop(0)
            assert uri == expected_uri
            resp = FakeResponse(
                code=200, phrase=b"OK", body=json.dumps(resp_obj).encode("utf-8"),
            )
            return resp

        with patch.object(self.hs.get_proxied_http_client(), "request", mock_req):
            # now hit the callback URI with the right params and a made-up code
            _, channel = make_request(
                self.hs.get_reactor(),
                self.site,
                "GET",
                redirect_uri,
                custom_headers=[
                    ("Cookie", "%s=%s" % (k, v)) for (k, v) in cookies.items()
                ],
            )

        # expect a confirmation page
        assert channel.code == 200

        # fish the matrix login token out of the body of the confirmation page
        m = re.search(
            'a href="%s.*loginToken=([^"]*)"' % (client_redirect_url,),
            channel.result["body"].decode("utf-8"),
        )
        assert m
        login_token = m.group(1)

        # finally, submit the matrix login token to the login API, which gives us our
        # matrix access token and device id.
        _, channel = make_request(
            self.hs.get_reactor(),
            self.site,
            "POST",
            "/login",
            content={"type": "m.login.token", "token": login_token},
        )
        assert channel.code == 200
        return channel.json_body


# an 'oidc_config' suitable for login_via_oidc.
TEST_OIDC_CONFIG = {
    "enabled": True,
    "discover": False,
    "issuer": "https://issuer.test",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": ["profile"],
    "authorization_endpoint": "https://z",
    "token_endpoint": "https://issuer.test/token",
    "userinfo_endpoint": "https://issuer.test/userinfo",
    "user_mapping_provider": {"config": {"localpart_template": "{{ user.sub }}"}},
}
