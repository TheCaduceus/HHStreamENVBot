# Taken from megadlbot_oss <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/webserver/routes.py>
# Thanks to Eyaadh <https://github.com/eyaadh>

import re
import time
import math
import logging
import secrets
import mimetypes
import time
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from WebStreamer import bot_loop
from functools import partial
from WebStreamer.bot import multi_clients, work_loads
from WebStreamer.server.exceptions import FIleNotFound, InvalidHash
from WebStreamer import Var, utils, StartTime, __version__, StreamBot
from concurrent.futures import ThreadPoolExecutor
import urllib.parse

THREADPOOL = ThreadPoolExecutor(max_workers=1000)

async def sync_to_async(func, *args, wait=True, **kwargs):
    pfunc = partial(func, *args, **kwargs)
    future = bot_loop.run_in_executor(THREADPOOL, pfunc)
    return await future if wait else future

routes = web.RouteTableDef()
@routes.get("/", allow_head=True)
async def root_route_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

@routes.get("/{path:.*}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        encrypted_code = urllib.parse.unquote(request.match_info['path'])
        logging.debug(f"Encrypted code Got: {encrypted_code}")

        parts = encrypted_code.split("/")
        if len(parts) != 4:
            raise web.HTTPBadRequest(text="Invalid path format")

        cid, fid, expiration_time, sha256_key = parts
        current_time = int(time.time())
        if int(expiration_time) < current_time:
            raise web.HTTPForbidden(text="Link is Expired")

        sha256_verified = await sync_to_async(utils.verify_sha256_key, cid, fid, expiration_time, sha256_key)
        if not sha256_verified:
            raise web.HTTPForbidden(text="Integrity check failed")

        return await media_streamer(request, int(fid), int(cid))
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FileNotFoundError as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        error_message = str(e)
        logging.critical(error_message)
        raise web.HTTPInternalServerError(text=error_message)

class_cache = {}

async def media_streamer(request: web.Request, message_id: int, channel_id):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if Var.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(message_id, channel_id)
    logging.debug("after calling get_file_properties")

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )
    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    if "video/" in mime_type or "audio/" in mime_type or "/html" in mime_type:
        disposition = "inline"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
