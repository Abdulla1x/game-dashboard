"""Pull the largest icon out of a Windows executable, using only the stdlib.

PowerShell's ExtractAssociatedIcon is hard-capped at 32x32, which is far too small to
build a cover from — it lands on the dashboard as a blurry smudge. Most real binaries
carry a 256x256 icon in their resource directory, so this reads the PE resource tree
directly and takes the biggest one.

Icon resources come in two flavours: modern ones are already PNG and are returned
untouched, older ones are a bottom-up DIB with a 1-bit transparency mask appended, which
is re-encoded here. Everything is done with `struct` and `zlib`.
"""

import os
import struct
import zlib

RT_ICON = 3

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class _Reader:
    """Random-access reads over a file, so a 200 MB game exe is never slurped."""

    def __init__(self, fh):
        self.fh = fh

    def at(self, offset, size):
        self.fh.seek(offset)
        data = self.fh.read(size)
        if len(data) != size:
            raise ValueError("read past end of file")
        return data

    def u16(self, offset):
        return struct.unpack("<H", self.at(offset, 2))[0]

    def u32(self, offset):
        return struct.unpack("<I", self.at(offset, 4))[0]


def _resource_section(r):
    """Return (rsrc_rva, rva_to_offset) for the PE's resource directory."""
    if r.at(0, 2) != b"MZ":
        raise ValueError("not a PE file")
    pe = r.u32(0x3C)
    if r.at(pe, 4) != b"PE\0\0":
        raise ValueError("bad PE signature")

    coff = pe + 4
    nsections = r.u16(coff + 2)
    opt_size = r.u16(coff + 16)
    opt = coff + 20
    magic = r.u16(opt)
    # PE32 puts 16 directory entries at +96, PE32+ at +112.
    dirs = opt + (96 if magic == 0x10B else 112)
    rsrc_rva = r.u32(dirs + 16)
    if not rsrc_rva:
        raise ValueError("no resource directory")

    sections = opt + opt_size
    table = []
    for i in range(nsections):
        s = sections + 40 * i
        vsize, vaddr = struct.unpack("<II", r.at(s + 8, 8))
        raw = r.u32(s + 20)
        table.append((vaddr, max(vsize, 1), raw))

    def to_offset(rva):
        for vaddr, vsize, raw in table:
            if vaddr <= rva < vaddr + vsize:
                return raw + (rva - vaddr)
        raise ValueError("rva outside every section")

    return rsrc_rva, to_offset


def _entries(r, table_offset):
    named, ident = struct.unpack("<HH", r.at(table_offset + 12, 4))
    out = []
    for i in range(named + ident):
        e = table_offset + 16 + 8 * i
        out.append(struct.unpack("<II", r.at(e, 8)))
    return out


def _icon_blobs(path):
    """Every RT_ICON resource in the file, as raw bytes."""
    with open(path, "rb") as fh:
        r = _Reader(fh)
        rsrc_rva, to_offset = _resource_section(r)
        root = to_offset(rsrc_rva)

        blobs = []
        for type_id, type_off in _entries(r, root):
            # High bit set means a name-based entry; icon types are numeric.
            if type_id & 0x80000000 or type_id != RT_ICON:
                continue
            if not type_off & 0x80000000:
                continue
            for _name_id, name_off in _entries(r, root + (type_off & 0x7FFFFFFF)):
                if not name_off & 0x80000000:
                    continue
                for _lang, leaf in _entries(r, root + (name_off & 0x7FFFFFFF)):
                    if leaf & 0x80000000:
                        continue  # a leaf must not point at another table
                    data_rva, size = struct.unpack("<II", r.at(root + leaf, 8))
                    if 0 < size <= 8 << 20:
                        blobs.append(r.at(to_offset(data_rva), size))
        return blobs


# -- DIB -> PNG ----------------------------------------------------------


def _png(width, height, rgba_rows):
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\0" + row for row in rgba_rows)  # filter type 0 per scanline
    return (
        _PNG_SIG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _dib_to_png(blob):
    """Convert an ICO-style bottom-up DIB (24/32bpp) to PNG bytes."""
    header = struct.unpack("<IiiHH", blob[:16])
    hsize, width, height, _planes, bpp = header
    if hsize < 40 or width <= 0:
        return None
    height //= 2  # the stored height covers the XOR image plus the AND mask
    if height <= 0 or bpp not in (24, 32):
        return None

    # Skip any colour table; 24/32bpp images normally have none.
    offset = hsize
    row_bytes = ((width * bpp + 31) // 32) * 4
    need = offset + row_bytes * height
    if len(blob) < need:
        return None

    mask_row = ((width + 31) // 32) * 4
    has_mask = len(blob) >= need + mask_row * height

    rows = []
    for y in range(height - 1, -1, -1):  # bottom-up
        src = offset + row_bytes * y
        out = bytearray(width * 4)
        if has_mask:
            mrow = blob[need + mask_row * y:need + mask_row * (y + 1)]
        for x in range(width):
            if bpp == 32:
                b, g, rr, a = blob[src + x * 4:src + x * 4 + 4]
            else:
                b, g, rr = blob[src + x * 3:src + x * 3 + 3]
                a = 255
            if has_mask and mrow:
                # AND mask: a set bit means "transparent".
                if (mrow[x >> 3] >> (7 - (x & 7))) & 1:
                    a = 0
            out[x * 4:x * 4 + 4] = bytes((rr, g, b, a))
        rows.append(bytes(out))

    # Some 32bpp icons leave the alpha channel entirely zero, which would render as a
    # fully transparent image. Treat that as "opaque" and let the AND mask decide.
    if bpp == 32 and not any(any(row[3::4]) for row in rows):
        opaque = []
        for row in rows:
            fixed = bytearray(row)
            fixed[3::4] = b"\xff" * width
            opaque.append(bytes(fixed))
        rows = opaque

    return _png(width, height, rows)


# -- PNG -> RGBA ---------------------------------------------------------


_UNPACK = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}  # colour type -> samples per pixel


def decode_png_rgba(data):
    """Decode an 8-bit, non-interlaced PNG to (width, height, RGBA bytes).

    Only what icons actually use is supported; anything else returns None. This exists
    so a colour can be sampled from an extracted icon to tint its generated cover.
    """
    if data[:8] != _PNG_SIG:
        return None

    pos = 8
    width = height = 0
    depth = ctype = interlace = 0
    palette = b""
    trns = b""
    idat = []

    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # length + tag + payload + crc

        if tag == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break

    if depth != 8 or interlace or ctype not in _UNPACK or not width or not height:
        return None

    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None

    channels = _UNPACK[ctype]
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return None

    # Undo the per-scanline filters (PNG spec 9.2).
    out = bytearray(stride * height)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        ftype = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            x = line[i]
            if ftype == 1:
                x += a
            elif ftype == 2:
                x += b
            elif ftype == 3:
                x += (a + b) >> 1
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                x += a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = x & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # Widen whatever we decoded to RGBA.
    rgba = bytearray(width * height * 4)
    for i in range(width * height):
        s = i * channels
        if ctype == 6:
            rgba[i * 4:i * 4 + 4] = out[s:s + 4]
        elif ctype == 2:
            rgba[i * 4:i * 4 + 3] = out[s:s + 3]
            rgba[i * 4 + 3] = 255
        elif ctype == 0:
            v = out[s]
            rgba[i * 4:i * 4 + 4] = bytes((v, v, v, 255))
        elif ctype == 4:
            v = out[s]
            rgba[i * 4:i * 4 + 4] = bytes((v, v, v, out[s + 1]))
        else:  # palette
            idx = out[s]
            if (idx + 1) * 3 <= len(palette):
                rgba[i * 4:i * 4 + 3] = palette[idx * 3:idx * 3 + 3]
            rgba[i * 4 + 3] = trns[idx] if idx < len(trns) else 255
    return width, height, bytes(rgba)


# -- public --------------------------------------------------------------


def _dimensions(blob):
    if blob[:8] == _PNG_SIG:
        return struct.unpack(">II", blob[16:24])
    if len(blob) >= 16:
        hsize, width, height = struct.unpack("<Iii", blob[:12])
        if hsize >= 40 and width > 0:
            return width, abs(height) // 2
    return 0, 0


def largest_icon_png(exe_native_path):
    """Best (largest) icon in `exe_native_path` as PNG bytes, or None."""
    if not exe_native_path or not os.path.isfile(exe_native_path):
        return None
    try:
        blobs = _icon_blobs(exe_native_path)
    except (ValueError, OSError, struct.error, IndexError):
        return None

    # Widest first; prefer a ready-made PNG when two entries tie on size.
    ranked = sorted(
        ((_dimensions(b), b) for b in blobs),
        key=lambda item: (item[0][0], item[1][:8] == _PNG_SIG),
        reverse=True,
    )
    for (width, _height), blob in ranked:
        if width < 32:
            continue
        if blob[:8] == _PNG_SIG:
            return blob
        try:
            png = _dib_to_png(blob)
        except (struct.error, IndexError, ValueError):
            png = None
        if png:
            return png
    return None


def write_largest_icon(exe_win_path, dest_png):
    """Extract to `dest_png`. Returns True on success."""
    import winpath

    png = largest_icon_png(winpath.native(exe_win_path))
    if not png:
        return False
    with open(dest_png, "wb") as fh:
        fh.write(png)
    return True
