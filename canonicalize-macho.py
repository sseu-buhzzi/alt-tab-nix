#!/usr/bin/env python3
"""Deterministic post-link canonicalizer for ld64 output.

ld64 emits non-deterministic CR binaries: the LC_UUID is content-hashed (so it
varies when anything else varies), and the ORDER of the dylib load commands
(and thus every ordinal reference in bind streams / the indirect symbol table)
varies per run. This normalizes a given Mach-O so that equal link INPUTS always
produce equal OUTPUT bytes:

  1. Sort LC_LOAD_DYLIB/LC_LOAD_WEAK_DYLIB commands: weak first (stable), then
     strong (stable), both by install name. Command sizes are preserved and the
     whole load-command region is rebuilt (non-dylib commands keep their
     relative order in front), so nothing else in the file shifts.
  2. Rewrite every ordinal reference:
     - dyld bind opcode streams (LC_DYLD_INFO/ONLY bind+weak+lazy): ordinals
       are re-encoded canonically. Encoding may grow/shrink, so the three
       streams are relocated to EOF (appended, aligned to 8) and the old slab
       is zeroed; the LC's offsets/sizes are patched.
     - indirect symbol table (LC_DYSYMTAB): in place, length is fixed.
     - chained fixups (LC_DYLD_CHAINED_FIXUPS): in place, length is fixed.
  3. Replace every LC_UUID with a hash of the (uuid-less) file.
"""

import hashlib
import struct
import sys

MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x80000018
LC_LOAD_UPWARD_DYLIB = 0x80000023
LC_REEXPORT_DYLIB = 0x8000001F
LC_UUID = 0x1B
LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x80000022
LC_DYLD_CHAINED_FIXUPS = 0x80000034

# LC_ID_DYLIB names the image itself; it is not a dependency ordinal.
DYLIB_CMDS = {
    LC_LOAD_DYLIB,
    LC_LOAD_WEAK_DYLIB,
    LC_LOAD_UPWARD_DYLIB,
    LC_REEXPORT_DYLIB,
}

# dyld bind opcodes (dyld source, BindConfigurator / MachOAnalyze)
BIND_DONE = 0x00
BIND_SET_DYLIB_ORDINAL_IMM = 0x10  # + imm (0..15)
BIND_SET_DYLIB_ORDINAL_ULEB = 0x20  # + uleb
BIND_SET_DYLIB_SPECIAL_IMM = 0x30  # + imm (negative ordinals: no rewrite)
BIND_SET_SYMBOL_TRAILING_FLAGS_IMM = 0x40  # + flags imm, + name\0
BIND_SET_TYPE_IMM = 0x50  # + imm
BIND_SET_ADDEND_SLEB = 0x60  # + sleb
BIND_SET_SEGMENT_AND_OFFSET_ULEB = 0x70  # + imm(segment), + uleb(offset)
BIND_ADD_ADDR_ULEB = 0x80  # + uleb
BIND_DO_BIND = 0x90  # (no operand)
BIND_DO_BIND_ADD_ADDR_ULEB = 0xA0  # + uleb
BIND_DO_BIND_ADD_ADDR_IMM_SCALED = 0xB0  # (no operand; imm scaled)
BIND_DO_BIND_ULEB_TIMES_SKIPPING_ULEB = 0xC0  # + uleb(count) + uleb(skip)
BIND_THREADED = 0xD0  # + sub-opcode byte (+ table/ulebs)


def remap_ordinal(x, perm):
    # dyld ordinals are 1-based; canonical rank is a permutation of 1..N
    if 1 <= x <= len(perm):
        return perm[x - 1] + 1
    return x


def read_uleb(d, o):
    r = 0
    s = 0
    while True:
        b = d[o]
        o += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            break
        s += 7
    return r, o


def uleb_bytes(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            b |= 0x80
        out.append(b)
        if not v:
            break
    return bytes(out)


def canonicalize_bind_stream(d, base, size, perm, threaded_ord_mask=0x7FFF):
    """Re-encode one dyld bind stream with canonical ordinals.
    Returns the new stream bytes (length may differ)."""
    o = base
    end = base + size
    out = bytearray()
    threaded_table_pos = None  # offset (into `out`) of the 16-bit ordinal table
    threaded_table_size = 0
    while o < end:
        op = d[o]
        kind = op & 0xF0
        imm = op & 0x0F
        if op == BIND_DONE:
            out.append(op)
            o += 1
        elif kind in (BIND_SET_DYLIB_ORDINAL_IMM, BIND_SET_DYLIB_ORDINAL_ULEB):
            if kind == BIND_SET_DYLIB_ORDINAL_IMM:
                ordv = imm
                o += 1
            else:
                ordv, o = read_uleb(d, o + 1)
            newv = remap_ordinal(ordv, perm)
            if newv <= 0xF:
                out.append(BIND_SET_DYLIB_ORDINAL_IMM | newv)
            else:
                out.append(BIND_SET_DYLIB_ORDINAL_ULEB)
                out += uleb_bytes(newv)
        elif kind == BIND_SET_DYLIB_SPECIAL_IMM:
            out.append(op)
            o += 1
        elif kind == BIND_SET_SYMBOL_TRAILING_FLAGS_IMM:
            out.append(op)
            o += 1
            while d[o] != 0:
                out.append(d[o])
                o += 1
            out.append(0)
            o += 1
        elif kind == BIND_SET_TYPE_IMM:
            out.append(op)
            o += 1
        elif kind == BIND_SET_ADDEND_SLEB:
            out.append(op)
            o += 1
            while True:
                b = d[o]
                out.append(b)
                o += 1
                if not (b & 0x80):
                    break
        elif kind == BIND_SET_SEGMENT_AND_OFFSET_ULEB:
            out.append(op)
            o += 1
            v, o2 = read_uleb(d, o)
            out += uleb_bytes(v)
            o = o2
        elif kind == BIND_ADD_ADDR_ULEB:
            out.append(op)
            o += 1
            v, o2 = read_uleb(d, o)
            out += uleb_bytes(v)
            o = o2
        elif op == BIND_DO_BIND:
            out.append(op)
            o += 1
        elif kind == BIND_DO_BIND_ADD_ADDR_ULEB:
            out.append(op)
            o += 1
            v, o2 = read_uleb(d, o)
            out += uleb_bytes(v)
            o = o2
        elif kind == BIND_DO_BIND_ADD_ADDR_IMM_SCALED:
            out.append(op)
            o += 1
        elif kind == BIND_DO_BIND_ULEB_TIMES_SKIPPING_ULEB:
            out.append(op)
            o += 1
            v1, o2 = read_uleb(d, o)
            v2, o3 = read_uleb(d, o2)
            out += uleb_bytes(v1)
            out += uleb_bytes(v2)
            o = o3
        elif kind == BIND_THREADED:
            sub = d[o + 1]
            out.append(op)
            out.append(sub)
            o += 2
            if sub == 0x00:  # SET_BIND_ORDINAL_TABLE_SIZE_ULEB
                v, o2 = read_uleb(d, o)
                threaded_table_size = v
                out += uleb_bytes(v)
                o = o2
                threaded_table_pos = len(out)
                out += bytes(v * 2)
            elif sub == 0x01:  # APPLY
                pass
            elif sub == 0x02:  # SET_SEGMENT_AND_OFFSET_ULEB
                seg = d[o]
                out.append(seg)
                o += 1
                v, o2 = read_uleb(d, o)
                out += uleb_bytes(v)
                o = o2
            else:
                raise SystemExit("unsupported threaded sub-opcode %#x" % sub)
        else:
            raise SystemExit(
                "unknown bind op %#x @ %d (off 0x%x)" % (op, o - base, base)
            )
    if o != end:
        raise SystemExit(
            "bind stream end mismatch: base=0x%x o=%d end=%d" % (base, o, end)
        )
    if threaded_table_pos is not None:
        tbl = out[threaded_table_pos : threaded_table_pos + threaded_table_size * 2]
        for i in range(threaded_table_size):
            e = tbl[2 * i] | (tbl[2 * i + 1] << 8)
            ordv = e & threaded_ord_mask
            newv = remap_ordinal(ordv, perm)
            if newv != ordv:
                e = (e & ~threaded_ord_mask) | (newv & threaded_ord_mask)
            tbl[2 * i] = e & 0xFF
            tbl[2 * i + 1] = (e >> 8) & 0xFF
    return bytes(out)


def process(path):
    d = bytearray(open(path, "rb").read())
    magic = struct.unpack_from("<L", d, 0)[0]
    if magic != MH_MAGIC_64:
        # fat (CAFEBABE/CAFEBABF/BEBAFECA) or 32-bit MH_MAGIC: not in scope
        print("skip %s (magic %#x)" % (path, magic))
        return False
    ncmds, sizeofcmds = struct.unpack_from("<LL", d, 16)
    hdrsize = 32
    cmds = []
    off = hdrsize
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<LL", d, off)
        cmds.append((cmd, off, size))
        off += size
    assert off == hdrsize + sizeofcmds

    # dylib load commands
    dylibs = []  # (index, cmd, off, size, name, weak)
    for i, (cmd, off, size) in enumerate(cmds):
        if cmd in DYLIB_CMDS:
            nameoff = struct.unpack_from("<L", d, off + 8)[0]
            base = off + nameoff
            end = d.index(b"\0", base)
            name = d[base:end].decode("utf-8", "replace")
            dylibs.append((i, cmd, off, size, name, cmd == LC_LOAD_WEAK_DYLIB))

    perm = list(range(max(len(dylibs), 1)))
    if dylibs:
        # canonical order: weak first, then strong, each stable by name
        ordered = sorted(dylibs, key=lambda x: (0 if x[5] else 1, x[4]))
        old_to_new = {id(x): n for n, x in enumerate(ordered)}
        perm = [old_to_new[id(x)] for x in dylibs]  # old_idx -> new_idx

        # ---- 0. rewrite ordinals in chained fixups (in place, fixed width) ----
        for cmd, off, size in cmds:
            if cmd == LC_DYLD_CHAINED_FIXUPS:
                dataoff, datasize = struct.unpack_from("<QQ", d, off + 8)
                base = dataoff
                starts_off = struct.unpack_from("<L", d, base + 8)[0]
                seg_count = struct.unpack_from("<L", d, base + starts_off)[0]
                for s in range(seg_count):
                    so = struct.unpack_from("<L", d, base + starts_off + 4 + 4 * s)[0]
                    if so == 0:
                        continue
                    so_abs = base + starts_off + so
                    page_size = struct.unpack_from("<L", d, so_abs + 4)[0]
                    fmt = struct.unpack_from("<L", d, so_abs + 8)[0]
                    page_count = struct.unpack_from("<H", d, so_abs + 20)[0]
                    if fmt != 1:
                        continue
                    for p in range(page_count):
                        pstart = struct.unpack_from("<H", d, so_abs + 22 + 2 * p)[0]
                        if pstart == 0xFFFF:
                            continue
                        w = so_abs + pstart
                        while True:
                            word = struct.unpack_from("<Q", d, w)[0]
                            bit = word & 3
                            tag = word & 0xF
                            if tag in (0x30, 0x31):
                                ordinal = (word >> 8) & (
                                    0xFFFFFFFF if tag == 0x30 else 0xFFF
                                )
                                neword = remap_ordinal(ordinal, perm)
                                if neword != ordinal:
                                    if tag == 0x30:
                                        word = (word & ~(0xFFFFFFFF << 8)) | (
                                            neword << 8
                                        )
                                    else:
                                        word = (word & ~(0xFFF << 8)) | (neword << 8)
                                    struct.pack_into("<Q", d, w, word)
                            if bit == 0 or w >= dataoff + datasize - 8:
                                break
                            w += bit

        # ---- 1. rewrite dyld bind/weak/lazy opcode streams (relocate to EOF) ----
        new_tables = b""
        sizes_new = []
        for cmd, off, size in cmds:
            if cmd in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY):
                lc_off = off
                (
                    rebase_off,
                    rebase_size,
                    bind_off,
                    bind_size,
                    weak_off,
                    weak_size,
                    la_off,
                    la_size,
                    export_off,
                    export_size,
                ) = struct.unpack_from("<IIIIIIIIII", d, off + 8)
                streams = [
                    (bind_off, bind_size),
                    (weak_off, weak_size),
                    (la_off, la_size),
                ]
                for b_off, b_size in streams:
                    if b_size:
                        nb = canonicalize_bind_stream(d, b_off, b_size, perm)
                        new_tables += nb
                        sizes_new.append(len(nb))
                    else:
                        sizes_new.append(0)
                # zero the old slab (bind..lazy end); deterministic
                slab_end = (
                    la_off + la_size
                    if la_size
                    else (weak_off + weak_size if weak_size else bind_off + bind_size)
                )
                if bind_size or weak_size or la_size:
                    d[bind_off:slab_end] = b"\0" * (slab_end - bind_off)
                # append new tables at EOF: streams are 8-aligned, so pad
                # bytes are materialized between them
                new_base = (len(d) + 7) & ~7
                padded = bytearray()
                cursor = new_base
                idx = 0
                for b_idx, b_size in enumerate(sizes_new):
                    if b_size:
                        align = (cursor + 7) & ~7
                        padded += b"\0" * (align - cursor)
                        padded += new_tables[idx : idx + b_size]
                        idx += b_size
                        cursor = align + b_size
                # zero any non-deterministic tail junk between the last real
                # linkedit table and where the new streams land
                table_ends = []
                for c2, o2, s2 in cmds:
                    if c2 == LC_SYMTAB:
                        symoff, nsyms, stroff, strsize = struct.unpack_from(
                            "<IIII", d, o2 + 8
                        )
                        table_ends += [symoff + 16 * nsyms, stroff + strsize]
                    elif c2 == LC_DYSYMTAB:
                        nind = struct.unpack_from("<L", d, o2 + 60)[0]
                        table_ends.append(
                            struct.unpack_from("<L", d, o2 + 56)[0] + 4 * nind
                        )
                    elif c2 in (
                        0x1E,
                        0x80000026,
                        0x80000027,
                        0x8000002E,
                        0x80000034,
                        0x80000035,
                        0x80000036,
                    ):
                        table_ends.append(
                            struct.unpack_from("<L", d, c2 + 8)[0]
                            + struct.unpack_from("<L", d, c2 + 12)[0]
                        )
                last_real = max(
                    table_ends + [rebase_off + rebase_size, export_off + export_size]
                )
                if new_base > last_real:
                    d[last_real:new_base] = b"\0" * (new_base - last_real)
                d += b"\0" * (new_base - len(d)) + padded
                # adhoc code signature blobs vary per build: neutralize them
                for c3, o3, s3 in cmds:
                    if c3 == 0x1D or c3 == 0x8000001D:
                        do, ds = struct.unpack_from("<LL", d, o3 + 8)
                        if ds:
                            d[do : do + ds] = b"\0" * ds
                            struct.pack_into("<LL", d, o3 + 8, 0, 0)
                # patch the LC dyld_info fields (rebase stays put)
                cursor = new_base
                for j, (name_, b_size) in enumerate(zip(("b", "w", "l"), sizes_new)):
                    if b_size:
                        cursor = (cursor + 7) & ~7
                        struct.pack_into("<II", d, lc_off + 16 + j * 8, cursor, b_size)
                        cursor += b_size
                final_eof = new_base + len(padded)
                # grow __LINKEDIT (the last segment) to cover the appended
                # streams; its size fields must match the final file extent
                for c3, o3, s3 in cmds:
                    if (
                        c3 == LC_SEGMENT_64
                        and d[o3 + 8 : o3 + 24].rstrip(b"\0") == b"__LINKEDIT"
                    ):
                        _, _, fileoff, filesize = struct.unpack_from(
                            "<QQQQ", d, o3 + 24
                        )
                        seg_end = fileoff + filesize
                        if final_eof > seg_end:
                            new_size = final_eof - fileoff
                            vmsize = (new_size + 0xFFFF) & ~0xFFFF
                            struct.pack_into("<Q", d, o3 + 32, vmsize)
                            struct.pack_into("<Q", d, o3 + 48, new_size)
                        break
                break

        # ---- 2. rewrite ordinals in indirect symbol table (in place) ----
        for cmd, off, size in cmds:
            if cmd == LC_DYSYMTAB:
                nindirect = struct.unpack_from("<L", d, off + 60)[0]
                isymoff = struct.unpack_from("<L", d, off + 56)[0]
                for i in range(nindirect):
                    e = struct.unpack_from("<L", d, isymoff + 4 * i)[0]
                    ordinal = (e >> 8) & 0xFF
                    newv = remap_ordinal(ordinal, perm)
                    if newv != ordinal:
                        e = (e & ~(0xFF << 8)) | (newv << 8)
                        struct.pack_into("<L", d, isymoff + 4 * i, e)

        # ---- 2b. rewrite ordinals in the symbol table n_desc (N_UNDF) ----
        # undefined symbols carry the library ordinal in bits 15..8 of n_desc
        for cmd, off, size in cmds:
            if cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", d, off + 8)
                for i in range(nsyms):
                    no = symoff + 16 * i
                    n_type = d[no + 4]
                    if n_type & 0x0E == 0x00:  # N_UNDF: n_desc holds the ordinal
                        e = struct.unpack_from("<H", d, no + 6)[0]
                        ordinal = (e >> 8) & 0xFF
                        newv = remap_ordinal(ordinal, perm)
                        if newv != ordinal:
                            struct.pack_into("<H", d, no + 6, (newv << 8) | (e & 0xFF))

        # ---- 3. rebuild the load-command region in canonical order ----
        # Non-dylib commands first (original relative order), then dylibs in
        # canonical order. LCs are position-independent (linkedit tables use
        # absolute offsets), and total size is unchanged so nothing shifts.
        rank_by_off = {x[2]: old_to_new[id(x)] for x in dylibs}
        order = sorted(
            range(len(cmds)),
            key=lambda i: (
                0 if cmds[i][0] in DYLIB_CMDS else 1,
                rank_by_off.get(cmds[i][1], 0),
                cmds[i][1],
            ),
        )
        blob = b"".join(bytes(d[c[1] : c[1] + c[2]]) for c in (cmds[i] for i in order))
        assert len(blob) == sizeofcmds
        d[hdrsize : hdrsize + sizeofcmds] = blob

        # ---- 4. replace every UUID with a hash of the (uuid-less) file ----
        # re-scan: offsets moved with the rebuild
        uuids = []
        off = hdrsize
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<LL", d, off)
            if cmd == LC_UUID:
                uuids.append(off)
            off += size
        for uoff in uuids:
            h = hashlib.sha256(d[: uoff + 8] + bytes(16) + d[uoff + 24 :]).digest()[:16]
            d[uoff + 8 : uoff + 24] = h

    open(path, "wb").write(d)
    return True


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(f"canonicalizing {p}")
        process(p)
