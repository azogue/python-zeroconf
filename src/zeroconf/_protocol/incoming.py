""" Multicast DNS Service Discovery for Python, v0.14-wmcbrine
    Copyright 2003 Paul Scott-Murphy, 2014 William McBrine

    This module provides a framework for the use of DNS Service Discovery
    using IP multicast.

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301
    USA
"""

import struct
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union, cast

from .._dns import (
    DNSAddress,
    DNSHinfo,
    DNSNsec,
    DNSPointer,
    DNSQuestion,
    DNSRecord,
    DNSService,
    DNSText,
)
from .._exceptions import IncomingDecodeError
from .._logger import log
from .._utils.time import current_time_millis
from ..const import (
    _FLAGS_QR_MASK,
    _FLAGS_QR_QUERY,
    _FLAGS_QR_RESPONSE,
    _FLAGS_TC,
    _TYPE_A,
    _TYPE_AAAA,
    _TYPE_CNAME,
    _TYPE_HINFO,
    _TYPE_NSEC,
    _TYPE_PTR,
    _TYPE_SRV,
    _TYPE_TXT,
    _TYPES,
)

DNS_COMPRESSION_HEADER_LEN = 1
DNS_COMPRESSION_POINTER_LEN = 2
MAX_DNS_LABELS = 128
MAX_NAME_LENGTH = 253

DECODE_EXCEPTIONS = (IndexError, struct.error, IncomingDecodeError)

UNPACK_3H = struct.Struct(b'!3H').unpack_from
UNPACK_6H = struct.Struct(b'!6H').unpack_from
UNPACK_HH = struct.Struct(b'!HH').unpack_from
UNPACK_HHiH = struct.Struct(b'!HHiH').unpack_from

_seen_logs: Dict[str, Union[int, tuple]] = {}


class DNSIncoming:
    """Object representation of an incoming DNS packet"""

    __slots__ = (
        "_did_read_others",
        'flags',
        'offset',
        'data',
        '_data_len',
        'name_cache',
        'questions',
        '_answers',
        'id',
        'num_questions',
        'num_answers',
        'num_authorities',
        'num_additionals',
        'valid',
        'now',
        'scope_id',
        'source',
    )

    def __init__(
        self,
        data: bytes,
        source: Optional[Tuple[str, int]] = None,
        scope_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> None:
        """Constructor from string holding bytes of packet"""
        self.flags = 0
        self.offset = 0
        self.data = data
        self._data_len = len(data)
        self.name_cache: Dict[int, List[str]] = {}
        self.questions: List[DNSQuestion] = []
        self._answers: List[DNSRecord] = []
        self.id = 0
        self.num_questions = 0
        self.num_answers = 0
        self.num_authorities = 0
        self.num_additionals = 0
        self.valid = False
        self._did_read_others = False
        self.now = now or current_time_millis()
        self.source = source
        self.scope_id = scope_id
        try:
            self._initial_parse()
        except DECODE_EXCEPTIONS:
            self._log_exception_debug(
                'Received invalid packet from %s at offset %d while unpacking %r',
                self.source,
                self.offset,
                self.data,
            )

    def is_query(self) -> bool:
        """Returns true if this is a query."""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_QUERY

    def is_response(self) -> bool:
        """Returns true if this is a response."""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_RESPONSE

    @property
    def truncated(self) -> bool:
        """Returns true if this is a truncated."""
        return (self.flags & _FLAGS_TC) == _FLAGS_TC

    def _initial_parse(self) -> None:
        """Parse the data needed to initalize the packet object."""
        self._read_header()
        self._read_questions()
        if not self.num_questions:
            self._read_others()
        self.valid = True

    @classmethod
    def _log_exception_debug(cls, *logger_data: Any) -> None:
        log_exc_info = False
        exc_info = sys.exc_info()
        exc_str = str(exc_info[1])
        if exc_str not in _seen_logs:
            # log the trace only on the first time
            _seen_logs[exc_str] = exc_info
            log_exc_info = True
        log.debug(*(logger_data or ['Exception occurred']), exc_info=log_exc_info)

    @property
    def answers(self) -> List[DNSRecord]:
        """Answers in the packet."""
        if not self._did_read_others:
            try:
                self._read_others()
            except DECODE_EXCEPTIONS:
                self._log_exception_debug(
                    'Received invalid packet from %s at offset %d while unpacking %r',
                    self.source,
                    self.offset,
                    self.data,
                )
        return self._answers

    def __repr__(self) -> str:
        return '<DNSIncoming:{%s}>' % ', '.join(
            [
                'id=%s' % self.id,
                'flags=%s' % self.flags,
                'truncated=%s' % self.truncated,
                'n_q=%s' % self.num_questions,
                'n_ans=%s' % self.num_answers,
                'n_auth=%s' % self.num_authorities,
                'n_add=%s' % self.num_additionals,
                'questions=%s' % self.questions,
                'answers=%s' % self.answers,
            ]
        )

    def _unpack(self, unpacker: Callable[[bytes, int], tuple], length: int) -> tuple:
        self.offset += length
        return unpacker(self.data, self.offset - length)

    def _read_header(self) -> None:
        """Reads header portion of packet"""
        (
            self.id,
            self.flags,
            self.num_questions,
            self.num_answers,
            self.num_authorities,
            self.num_additionals,
        ) = self._unpack(UNPACK_6H, 12)

    def _read_questions(self) -> None:
        """Reads questions section of packet"""
        self.questions = [
            DNSQuestion(self._read_name(), *self._unpack(UNPACK_HH, 4)) for _ in range(self.num_questions)
        ]

    def _read_character_string(self) -> bytes:
        """Reads a character string from the packet"""
        length = self.data[self.offset]
        self.offset += 1
        return self._read_string(length)

    def _read_string(self, length: int) -> bytes:
        """Reads a string of a given length from the packet"""
        info = self.data[self.offset : self.offset + length]
        self.offset += length
        return info

    def _read_others(self) -> None:
        """Reads the answers, authorities and additionals section of the
        packet"""
        self._did_read_others = True
        n = self.num_answers + self.num_authorities + self.num_additionals
        for _ in range(n):
            domain = self._read_name()
            type_, class_, ttl, length = UNPACK_HHiH(self.data, self.offset)
            self.offset += 10
            end = self.offset + length
            rec = None
            try:
                rec = self._read_record(domain, type_, class_, ttl, length)
            except DECODE_EXCEPTIONS:
                # Skip records that fail to decode if we know the length
                # If the packet is really corrupt read_name and the unpack
                # above would fail and hit the exception catch in read_others
                self.offset = end
                log.debug(
                    'Unable to parse; skipping record for %s with type %s at offset %d while unpacking %r',
                    domain,
                    _TYPES.get(type_, type_),
                    self.offset,
                    self.data,
                    exc_info=True,
                )
            if rec is not None:
                self._answers.append(rec)

    def _read_record(self, domain, type_: int, class_: int, ttl: int, length: int) -> Optional[DNSRecord]:  # type: ignore[no-untyped-def]
        """Read known records types and skip unknown ones."""
        if type_ == _TYPE_A:
            return DNSAddress(domain, type_, class_, ttl, self._read_string(4), created=self.now)
        if type_ in (_TYPE_CNAME, _TYPE_PTR):
            return DNSPointer(domain, type_, class_, ttl, self._read_name(), self.now)
        if type_ == _TYPE_TXT:
            return DNSText(domain, type_, class_, ttl, self._read_string(length), self.now)
        if type_ == _TYPE_SRV:
            return DNSService(
                domain,
                type_,
                class_,
                ttl,
                *cast(Tuple[int, int, int], self._unpack(UNPACK_3H, 6)),
                self._read_name(),
                self.now,
            )
        if type_ == _TYPE_HINFO:
            return DNSHinfo(
                domain,
                type_,
                class_,
                ttl,
                self._read_character_string().decode('utf-8'),
                self._read_character_string().decode('utf-8'),
                self.now,
            )
        if type_ == _TYPE_AAAA:
            return DNSAddress(
                domain, type_, class_, ttl, self._read_string(16), created=self.now, scope_id=self.scope_id
            )
        if type_ == _TYPE_NSEC:
            name_start = self.offset
            return DNSNsec(
                domain,
                type_,
                class_,
                ttl,
                self._read_name(),
                self._read_bitmap(name_start + length),
                self.now,
            )
        # Try to ignore types we don't know about
        # Skip the payload for the resource record so the next
        # records can be parsed correctly
        self.offset += length
        return None

    def _read_bitmap(self, end: int) -> List[int]:
        """Reads an NSEC bitmap from the packet."""
        rdtypes = []
        while self.offset < end:
            window = self.data[self.offset]
            bitmap_length = self.data[self.offset + 1]
            for i, byte in enumerate(self.data[self.offset + 2 : self.offset + 2 + bitmap_length]):
                for bit in range(0, 8):
                    if byte & (0x80 >> bit):
                        rdtypes.append(bit + window * 256 + i * 8)
            self.offset += 2 + bitmap_length
        return rdtypes

    def _read_name(self) -> str:
        """Reads a domain name from the packet."""
        labels: List[str] = []
        seen_pointers: Set[int] = set()
        original_offset = self.offset
        self.offset = self._decode_labels_at_offset(original_offset, labels, seen_pointers)
        self.name_cache[original_offset] = labels
        name = ".".join(labels) + "."
        if len(name) > MAX_NAME_LENGTH:
            raise IncomingDecodeError(
                f"DNS name {name} exceeds maximum length of {MAX_NAME_LENGTH} from {self.source}"
            )
        return name

    def _decode_labels_at_offset(self, off: int, labels: List[str], seen_pointers: Set[int]) -> int:
        # This is a tight loop that is called frequently, small optimizations can make a difference.
        while off < self._data_len:
            length = self.data[off]
            if length == 0:
                return off + DNS_COMPRESSION_HEADER_LEN

            if length < 0x40:
                label_idx = off + DNS_COMPRESSION_HEADER_LEN
                labels.append(self.data[label_idx : label_idx + length].decode('utf-8', 'replace'))
                off += DNS_COMPRESSION_HEADER_LEN + length
                continue

            if length < 0xC0:
                raise IncomingDecodeError(
                    f"DNS compression type {length} is unknown at {off} from {self.source}"
                )

            # We have a DNS compression pointer
            link = (length & 0x3F) * 256 + self.data[off + 1]
            if link > self._data_len:
                raise IncomingDecodeError(
                    f"DNS compression pointer at {off} points to {link} beyond packet from {self.source}"
                )
            if link == off:
                raise IncomingDecodeError(
                    f"DNS compression pointer at {off} points to itself from {self.source}"
                )
            if link in seen_pointers:
                raise IncomingDecodeError(
                    f"DNS compression pointer at {off} was seen again from {self.source}"
                )
            linked_labels = self.name_cache.get(link, [])
            if not linked_labels:
                seen_pointers.add(link)
                self._decode_labels_at_offset(link, linked_labels, seen_pointers)
                self.name_cache[link] = linked_labels
            labels.extend(linked_labels)
            if len(labels) > MAX_DNS_LABELS:
                raise IncomingDecodeError(
                    f"Maximum dns labels reached while processing pointer at {off} from {self.source}"
                )
            return off + DNS_COMPRESSION_POINTER_LEN

        raise IncomingDecodeError("Corrupt packet received while decoding name from {self.source}")
