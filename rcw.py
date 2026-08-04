#!/usr/bin/env python3

# rcw.py -- compiles an RCW source file into an PBL/RCW binary

# Copyright 2017-2019 NXP
# Author: Timur Tabi <timur@freescale.com>
# Further updates: Heinz Wrobel <Heinz.Wrobel@nxp.com>

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#     notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#     * Neither the name of Freescale Semiconductor nor the
#     names of its contributors may be used to endorse or promote products
#     derived from this software without specific prior written permission.

# This software is provided by Freescale Semiconductor "as is" and any
# express or implied warranties, including, but not limited to, the implied
# warranties of merchantability and fitness for a particular purpose are
# disclaimed. In no event shall Freescale Semiconductor be liable for any
# direct, indirect, incidental, special, exemplary, or consequential damages
# (including, but not limited to, procurement of substitute goods or services;
# loss of use, data, or profits; or business interruption) however caused and
# on any theory of liability, whether in contract, strict liability, or tort
# (including negligence or otherwise) arising in any way out of the use of
# this software, even if advised of the possibility of such damage.

# This program compiles an RCW "source file" into a binary image, with or
# without a PBL header.  The syntax of the source file is very simple.  These
# are the only constructs supported:
#
# 1. A bit field definition.  Bit fields are defined in the format XXX[b:e],
# where XXX is the name of the field (only letters, numbers, and an underscore
# are allowed), 'b' is the starting bit position, and 'e' is the ending bit
# position. A declaration of [0:5] is different from [5:0] in bit sequence!
#
# 2. A value assigned to a bit field.  Assignments are defined in the format
# XXX=v, where XXX is the name of the field (which must have been previously
# defined), and 'v' is the numeric value assigned to that field.
#
# 3. A variable, which begins with a % symbol.  Any variable can by created, and
# it can be assigned any string.  However, specific variables have pre-defined
# meanings.  Variables are used to define parts of the RCW or PBL that cannot
# be defined by a symbol assignment.
#
# Examples for use of special variables:
#       %loadwochecksum         -- Set to 1 if RCW need to be loaded without performing checksum
#       %size=1024              -- Must be set to RCW bit count
#       %pbiformat=2            -- Must be set to 2 for LS2 platform PBI
#       %nocrc=1                -- Must be set to 1 if only 'STOP' command 
#                                  is required instead of 'CRC and STOP' cmd
#       %classicbitnumbers=1    -- Non-Power Architecture bit numbering
#       %littleendian=1         -- Needed for LS2 style platform
#       %littleendian64b=1      -- Swaps eight bytes instead of four
#       %dont64bswapcrc=1       -- Can be set if CRC should stay normal
#                                  for %littleendian64b=1
#       %sysaddr, %pbladdr      -- Hex needed for pbiformat=1
#
#
# 4. A PBI can be defined in a .pbi/.end block.  Start the section with a line
# containing the string ".pbi".  The following PBI commands are available:
#
#   wait <n>        -- wait <n> cycles
#   write <a> <v>   -- write value <v> to address <a>
#   awrite <a> <v>  -- write value <v> to address <a>, with ACS bit set
#   flush           -- flush (perform a read at the addr of the previous write)
#   loadacwindow    -- LS2 family PBI, one arg
#   poll[.long]     -- LS2 family PBI, three args
#   blockcopy       -- LS2 family PBI, four args
#
# Terminate the PBI section with ".end".
#
# The C pre-processor is invoked prior to parsing the source file.  This allows
# for C/C++ style comments and macros to be used in the source file.
#
# This program is provided as an informal tool for evaluation purposes only.
# It is not supported.  Please contact the author directly with any questions.

import os
import re
import sys
import struct
import binascii
import itertools
import subprocess

from optparse import OptionParser, OptionGroup

# An ordered dictionary.  This is like a normal dictionary, except that it's
# possible to iterate over the contents in the same order that they were added
# to the dictionary.  This allows us to display the entries in the .rcw file
# in the same order that they appear in the .rcwi file.
class ordered_dict(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self._order = list(self.keys())

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._order.remove(key)

    def order(self):
        return self._order[:]

    def ordered_items(self):
        return [(key,self[key]) for key in self._order]

# Python's binascii.crc32() function uses a different algorithm to calculate
# the CRC, so we need to do it manually.  The polynomial value is 0x04c11db7.
# Python integers can be larger than 32 bits, so we have to "& 0xffffffff"
# to keep the value within a 32-bit range.  The CRC algorithm depends on this.
# Otherwise, some calculations may overflow.
def crc32(data):
    # Calculate the CRC table
    table = []
    for i in range(256):
        mask = i << 24
        for j in range(8):
            if mask & 0x80000000:
                mask = (mask << 1) ^ 0x04c11db7;
            else:
                mask <<= 1;
        table.append(mask & 0xffffffff)

    crc = 0xffffffff
    for i in data:
        crc = (crc << 8) ^ table[(crc >> 24) ^ int(i)]
        crc = crc & 0xffffffff

    return crc

# Command-line parser
def command_line():
    global options, args

    parser = OptionParser(usage='usage: %prog [options]',
        description='This script reads an RCW source file and generates an '
            'RCW bin file.')

    parser.add_option('-i', dest='input', help='input filename.  '
        'Defaults to stdin if not specified')

    parser.add_option('-o', dest='output', help='output filename.  '
        'Defaults to stdout if not specified')

    parser.add_option('--no-pbl', dest='pbl',
        help='do not generate the PBL preamble and end-command',
        action='store_false', default=True)

    parser.add_option('-r', dest='reverse', help='generate a source file from a binary.  '
        'Must also specify --rcwi.  --pbl option is ignored.', action='store_true',
            default=False)

    parser.add_option('-I', dest='include', help='include path.  '
        'Can be specified multiple times', action="append")

    parser.add_option('--rcwi', dest='rcwi', help='RCWI definition filename.  '
        'Used only if -r is specified.')

    parser.add_option('-w', dest='warnings', help='enable warning messages',
        action='store_true', default=False)

    parser.add_option('-D', dest='bitfields', help='bitfield definition. '
        'Can be specified multiple times.  Defines a value for a bitfield, '
        'overriding what is specified in the source file. '
        'Example: "-D MEM_PLL_RAT=14".',
        action='append', default=[])

    (options, args) = parser.parse_args()

    if options.input:
        options.input = os.path.expanduser(options.input)
    else:
        options.input = '/dev/stdin'

    if options.output:
        options.output = os.path.expanduser(options.output)
    else:
        options.output = '/dev/stdout'

    if options.reverse and not options.rcwi:
        print("Error: -r option requires --rcw")
        sys.exit(1)

# Checks if the bits for the given field overlap those of another field that
# we've already parsed.
def check_for_overlap(name, begin, end):
    global symbols

    if name in symbols:
        print('Error: Duplicate bitfield definition for', name)
        return

    # Iterate over the list of symbols that have already been defined
    for n, [b, e] in symbols.items():
        # check if either 'begin' or 'end' is inside an bitfield range
        if (b <= begin <= e) or (b <= end <= e):
            print('Error: Bitfield', name, 'overlaps with', n)

def _get_var_int(name, default=0):
    """Return vars[name] as int, or default if absent."""
    return int(vars[name], 0) if name in vars else default

#
# Build a u-boot PBI section for SPI/SD/NAND boot
#         refer: Chapter 10, u-boot of QorIQ_SDK_Infocenter.pdf
#
# pre-cond 1: u-boot.xxd should be created
# how to create u-boot.xxd
#         xxd u-boot.bin > u-boot.xxd1 && cut -d " " -f1-10 u-boot.xxd1 > u-boot.xxd && rm -f u-boot.xxd1
#
# rcw file should include spi_boot.rcw as well
#
def build_pbi_uboot(lines):
    subsection = b''
    cnt = 1
    l_tmp = []

    # files fed in is done by xxd and cut
    for l in lines:
        # prepare 0x40 per lines except the last one
        # add flush at the end 
        lstr = l.split()
        addr = int(lstr[0][:-1], 16)
        
        # print l
        #
        # last two lines take  0x20 numbers
        #
        if ((cnt % 2 == 0) and (cnt > len(lines) -4)):
            l_tmp.append(l)
            b = []

            for t in l_tmp:
                lstr = t.split()

                for i in range(1, len(lstr)):
                    b.append(int(lstr[i], 16))

            subsection += struct.pack('>LHHHHHHHHHHHHHHHH',\
                0x0C1F80000 + (addr - 0x10),\
                b[0],  b[1],  b[2],  b[3],  b[4],  b[5],  b[6],  b[7],\
                b[8],  b[9],  b[10], b[11], b[12], b[13], b[14], b[15])
            l_tmp = []
        #
        # the rest of lines take 0x40 numbers
        elif (cnt % 4 == 0):
            l_tmp.append(l)
            b = []
            for t in l_tmp:
                lstr = t.split()
                for i in range(1, len(lstr)):
                    b.append(int(lstr[i], 16))

            subsection += struct.pack('>LHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH',\
                0x081F80000 + (addr - 0x30),\
                b[0],  b[1],  b[2],  b[3],  b[4],  b[5],  b[6],  b[7],\
                b[8],  b[9],  b[10], b[11], b[12], b[13], b[14], b[15],\
                b[16], b[17], b[18], b[19], b[20], b[21], b[22], b[23], \
                b[24], b[25], b[26], b[27], b[28], b[29], b[30], b[31])
            l_tmp = []
        else:
            l_tmp.append(l)

        cnt = cnt + 1

    return subsection

def build_pbi_loadc(endianess, addr, mask):
    return (struct.pack(endianess + 'L', 0x80140000) +
            struct.pack(endianess + 'L', addr) +
            struct.pack(endianess + 'L', mask))

def build_pbi_jump(endianess, offset):
    return (struct.pack(endianess + 'L', 0x80840000) +
            struct.pack(endianess + 'L', offset))

def build_pbi_jumpc(endianess, condition, offset):
    return (struct.pack(endianess + 'L', 0x80850000) +
            struct.pack(endianess + 'L', condition) +
            struct.pack(endianess + 'L', offset))

def build_pbi_poll(endianess, addr, mask, condition, long=False):
    # Poll Short (0x80) or Poll Long (0x81) - RM §8.3.11
    # Word 0: header=0x80 | cmd=0x80/0x81 | rsvd=0x0000
    # Word 1: addr
    # Word 2: mask
    # Word 3: condition
    cmd = 0x81 if long else 0x80
    return (struct.pack(endianess + 'L', 0x80000000 | (cmd << 16)) +
            struct.pack(endianess + 'L', addr) +
            struct.pack(endianess + 'L', mask) +
            struct.pack(endianess + 'L', condition))

# Build a PBI section
def build_pbi(lines):
    subsection = b''
    global vars

    pbiformat = _get_var_int('pbiformat')
    endianess = ">"
    if _get_var_int('littleendian'):
        endianess = "<"

    for l in lines:
        # Check for an instruction without 0-3 parameters
        # The + ' ' is a hack to make the regex work for just 'flush'
        m = re.match(r'\s*([a-z]+)(|\.b1|\.b2|\.b4|\.b5|\.short|\.long)\s*(?<=\s)([^,]*),?([^,]*),?([^,]*),?([^,]*),?([^,]*)', l.decode("ascii") + ' ')
        if not m:
            print('Unknown PBI subsection command "%s"' % l)
            return ''
        op = m.group(1)
        opsize = m.group(2)
        opsizebytes = 3
        if opsize == '.b1':
            opsizebytes = 1

        p1 = m.group(3).strip()
        p2 = m.group(4).strip()
        p3 = m.group(5).strip()
        p4 = m.group(6).strip()
        p5 = m.group(7).strip()
        p1 = eval(p1, {"__builtins__":None}, {}) if len(p1) else None
        p2 = eval(p2, {"__builtins__":None}, {}) if len(p2) else None
        p3 = eval(p3, {"__builtins__":None}, {}) if len(p3) else None
        p4 = eval(p4, {"__builtins__":None}, {}) if len(p4) else None
        p5 = eval(p5, {"__builtins__":None}, {}) if len(p5) else None
        if op == 'wait':
            if p1 == None:
                print('Error: "wait" instruction requires one parameter')
                return ''
            if pbiformat == 2:
                v1 = struct.pack(endianess + 'L', 0x80820000 | p1)
                subsection += v1
            else:
                v1 = struct.pack(endianess + 'L', 0x090000c0 |  (int(vars['pbladdr'], 16) & 0x00ffff00))
                v2 = struct.pack(endianess + 'L', p1)
                subsection += v1
                subsection += v2
        elif op == 'write':
            if p1 == None or p2 == None:
                print('Error: "write" instruction requires two parameters')
                return ''
            if pbiformat == 2:
                v1 = struct.pack(endianess + 'L', (opsizebytes << 28) | p1)
            else:
                v1 = struct.pack(endianess + 'L', 0x09000000 + p1)
            v2 = struct.pack(endianess + 'L', p2)
            subsection += v1
            subsection += v2
        elif op == 'loadc':
            if pbiformat != 2:
                print('Error: "loadc" not supported for old PBI format')
                return ''
            if p1 == None or p2 == None:
                print('Error: "loadc" instruction requires two parameters')
                return ''
            subsection += build_pbi_loadc(endianess, p1, p2)
        elif op == 'jumpc':
            if pbiformat != 2:
                print('Error: "jumpc" not supported for old PBI format')
                return ''
            if p1 == None or p2 == None:
                print('Error: "jumpc" instruction requires two parameters')
                return ''
            subsection += build_pbi_jumpc(endianess, p1, p2)
        elif op == 'jump':
            if p1 == None:
                print('Error: "jump" instruction requires a parameter')
                return ''
            subsection += build_pbi_jump(endianess, p1)
        elif op == 'awrite':
            if opsize == '.b5':
                # altconfig write with B=5 (16 bytes)
                # write <v1>,<v2>,<v3>,<v4> to address starting <a>
                opsizebytes = 5
                if pbiformat != 2:
                    print('Error: "awrite.b5" not support for old PBI format')
                    return ''

                if p1 == None or p2 == None or p3 == None or p4 == None or p5 == None:
                    print('Error: "awrite.b5" instruction requires 5 parameters')
                    return ''

                v1 = struct.pack(endianess + 'L', 0x80000000 + (opsizebytes << 26) + p1)
                v2 = struct.pack(endianess + 'L', p2)
                v3 = struct.pack(endianess + 'L', p3)
                v4 = struct.pack(endianess + 'L', p4)
                v5 = struct.pack(endianess + 'L', p5)
                subsection += v1
                subsection += v2
                subsection += v3
                subsection += v4
                subsection += v5
            elif opsize == '.b4':
                # altconfig write with B=4 (8 bytes)
                # write <v1>,<v2> to address starting <a>
                opsizebytes = 4
                if pbiformat != 2:
                    print('Error: "awrite.b4" not support for old PBI format')
                    return ''

                if p1 == None or p2 == None or p3 == None:
                    print('Error: "awrite.b5" instruction requires 3 parameters')
                    return ''

                v1 = struct.pack(endianess + 'L', 0x80000000 + (opsizebytes << 26) + p1)
                v2 = struct.pack(endianess + 'L', p2)
                v3 = struct.pack(endianess + 'L', p3)
                subsection += v1
                subsection += v2
                subsection += v3
            else:
                if p1 == None or p2 == None:
                    print('Error: "awrite" instruction requires two parameters')
                    return ''
                if pbiformat == 2:
                    v1 = struct.pack(endianess + 'L', 0x80000000 + (opsizebytes << 26) + p1)
                else:
                    v1 = struct.pack(endianess + 'L', 0x89000000 + p1)
                v2 = struct.pack(endianess + 'L', p2)
                subsection += v1
                subsection += v2
        elif op == 'poll':
            if pbiformat != 2:
                print('Error: "poll" not support for old PBI format')
                return ''
            if p1 == None or p2 == None or p3 == None:
                print('Error: "poll" instruction requires three parameters')
                return ''
            subsection += build_pbi_poll(endianess, p1, p2, p3, long=(opsize == '.long'))
        elif op == 'loadacwindow':
            if pbiformat != 2:
                print('Error: "loadacwindow" not supported for old PBI format')
                return ''
            if p1 == None:
                print('Error: "loadacwindow" instruction requires one parameter')
                return ''
            v1 = struct.pack(endianess + 'L', 0x80120000 + p1)
            subsection += v1
        elif op == 'blockcopy':
            if pbiformat != 2:
                print('Error: "blockcopy" not supported for old PBI format')
                return ''
            if p1 == None or p2 == None or p3 == None or p4 == None:
                print('Error: "blockcopy" instruction requires four parameters')
                return ''
            v1 = struct.pack(endianess + 'L', 0x80000000 + (p1 & 0xff))
            v2 = struct.pack(endianess + 'L', p2)
            v3 = struct.pack(endianess + 'L', p3)
            v4 = struct.pack(endianess + 'L', p4)
            subsection += v1
            subsection += v2
            subsection += v3
            subsection += v4
        elif op == 'flush':
            subsection += struct.pack('>LL', 0x09000000 | (int(vars['pbladdr'], 16) & 0x00ffff00), 0)
        else:
            print('Unknown PBI subsection command "%s"' % l)
            return ''

    return subsection

# Parse a subsection
def parse_subsection(header, lines):
    if header == "pbi":
        return build_pbi(lines)
    elif header == "uboot":
        return build_pbi_uboot(lines)

    print('Error: unknown subsection "%s"' % header)
    return ''

# Parse the .rcw file, one line at a time
def parse_source_file(source):
    global options
    global symbols
    global assignments
    global vars
    global pbi

    symbols = ordered_dict()

    in_subsection = False   # True == we're in a subsection
    pbi = b''

    for l2 in source:
        l = re.sub(r'\s+', '', l2.decode("ascii")) # Remove all whitespace

        if not len(l):  # Skip blank or comment-only lines
            continue

        # Is it a subsection?
        m = re.match(r'\.([a-zA-Z]+)', l)
        if m:
            if in_subsection:
                in_subsection = False
                pbi += parse_subsection(header, s)
            else:
                in_subsection = True
                header = m.group(1)
                s = []
            continue

        # Is it a subsection line?
        if in_subsection:
            s.append(l2.strip())
            continue

        # Is it an identifier?  %var=value
        m = re.match(r'%([a-zA-Z]+[a-zA-Z0-9]+)=(.+)', l)
        if m:
            identifier, value = m.groups()
            vars[identifier] = value
            continue

        # Is it a single field definition?  NAME[position]
        m = re.match(r'([A-Z0-9_]+)\[([0-9a-zA-Z]+)]', l)
        if m:
            name, position = m.groups()
            position = int(position, 0)
            check_for_overlap(name, position, position)

            symbols[name] = [position, position]
            continue

        # Is it a ranged field definition?  NAME[begin:end]
        m = re.match(r'([A-Z0-9_]+)\[([0-9a-zA-Z]+):([0-9a-zA-Z]+)\]', l)
        if m:
            (name, begin, end) = m.groups()
            begin = int(begin, 0)
            end = int(end, 0)
            check_for_overlap(name, begin, end)
            symbols[name] = [begin, end]
            continue

        # Is it a field assignment? NAME=value
        m = re.match(r'([A-Z0-9_]+)=([0-9a-zA-Z]+)', l)
        if m:
            (name, value) = m.groups()
            value = int(value, 0)
            if not name in symbols:
                print('Error: Unknown bitfield', name)
            else:
                if options.warnings and (name in assignments):
                    print('Warning: Duplicate assignment for bitfield', name)
                assignments[name] = value
            continue

        print('Error: unknown command', ' '.join(l2))

# Parse the -D command line parameter for additional bitfield assignments
def parse_cmdline_bitfields():
    global options
    global assignments

    for l in options.bitfields:
        # This is the same regex as used in parse_source_file()
        m = re.search(r'([A-Z0-9_]+)=([0-9a-zA-Z]+)', l)
        if not m:
            print('Unrecognized command-line bitfield:', l)
        else:
            (name, value) = m.groups()
            value = int(value, 0)
            if not name in symbols:
                print('Error: Unknown bitfield', name)
            else:
                # Don't bother printing a warning, since the command-line will
                # normally be used to overwrite values in the .rcw file
                assignments[name] = value

# Return True if an executable program exists in the PATH somewhere
def find_program(filename):
    for path in os.environ["PATH"].split(os.pathsep):
        file = os.path.join(path, filename)
        if os.path.isfile(file) and os.access(file, os.X_OK):
            return True

    return False

# Run the C preprocessor on the given source code.  This allows you to include
# C macros and #include statements in the source file.
def read_source_file(filename):
    global options

    if not find_program('gcc'):
        print('Could not find gcc in PATH')
        return None

    i = ['-I', '.']     # Always look in the current directory
    if options.include:
        for x in options.include:
            i.extend(['-I', x])
    p = subprocess.Popen(['gcc', '-E', '-x', 'c', '-P'] + i + [filename],
        shell=False, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ret = p.communicate()
    if p.returncode != 0:
        print(ret[1],)
        return None

    return ret[0].splitlines()

# Check for specific variables
def check_vars():
    global vars
    global options

    if not 'size' in vars:
        print('Error: "%size" variable must be specified')
        sys.exit(1)

    if options.pbl:
        if _get_var_int('pbiformat') == 2:
            if 'sysaddr' in vars:
                print('Error: PBL format does not use %sysaddr')
                sys.exit(1)
            #if 'pbladdr' in vars:
            #    print 'Error: PBL format does not use %pbladdr'
            #    sys.exit(1)
        else:
            # If we want the PBL header/footer, the vars for those must be defined
            if not 'sysaddr' in vars:
                print('Error: PBL format requires %sysaddr to be defined')
                sys.exit(1)
            if _get_var_int('nocrc'):
                print('Error: %nocrc=1 is not supported for legacy PBI format; '
                      'use %pbiformat=2')
                sys.exit(1)

# Create a .bin file
def create_binary():
    global symbols
    global assignments
    global vars
    global options
    global pbi

    # Create the RCW data.  We encode it into 'bits' as a giant (2^size)-bit number
    nocrc = _get_var_int('nocrc')

    # check for load without checksum
    loadwochecksum = _get_var_int('loadwochecksum')

    size = int(vars['size'], 0)
    pbiformat = _get_var_int('pbiformat')
    classicbitnumbers = _get_var_int('classicbitnumbers')
    endianess = ">"
    if _get_var_int('littleendian'):
        endianess = "<"
    endianess64b = _get_var_int('littleendian64b')
    dont64bswapcrc = _get_var_int('dont64bswapcrc')
    bits = 0

    # Magic hack. If a pbi is specified and we didn't set the size,
    # set it for the new format!
    if 'PBI_LENGTH' in symbols and not 'PBI_LENGTH' in assignments:
        if pbiformat == 2:
            pbilen = len(pbi) / 4
            if options.pbl:
                # CRC and Stop adds two words
                pbilen += 2
            assignments['PBI_LENGTH'] = pbilen
            
    for n, v in assignments.items():
        # n = name of symbol
        # v = value to assign
        bb, ee = symbols[n]   # First bit and last bit
        b = min(bb, ee)
        e = max(bb, ee)
        s = 1 + e - b       # number of bits in field

        # Make sure it's not too large
        if v >= (1 << s):
            print('Error: Value', v, 'is too large for field', n)
            continue

        # If we treat the bitfield as "classic" numbered, reverse
        # the value before adding it!
        if b != bb:
            v = int(bin(int(v))[2:].zfill(s)[::-1], 2)
                
        # Set the bits.  We assume that bits [b:e] are already zero.  They can be
        # non-zero only if we have overlapping bitfield definitions, which we
        # already report as an error.
        bits += v << ((size - 1) - e)

    # Generate the binary.  First, apply the preamble, if requested
    binary = b''
    if options.pbl:
        # Starting with LS2, we have a larger field and a different
        # format.
        binary = struct.pack(endianess + 'L', 0xaa55aa55)
        if pbiformat == 2:
            if loadwochecksum == 1:
                # Load RCW w/o checksum command
                binary += struct.pack(endianess + 'L', 0x80110000)
            else:
                # Load RCW with checksum command
                binary += struct.pack(endianess + 'L', 0x80100000)
        else:
            length_byte = (((size // 8) & 63) << 1) | 1
            binary += struct.pack(endianess + 'L', (length_byte << 24) | (int(vars['sysaddr'], 16) & 0x00ffffff))

    # Then convert 'bits' into an array of bytes
    for i in range(size - 8, -1, -8):
        byte = bits >> i & 0xff
        if classicbitnumbers:
            byte = int(bin(byte)[2:].zfill(8)[::-1], 2)
        binary += bytes([byte])

    if options.pbl:
        if pbiformat == 2:
            if loadwochecksum == 1:
                binary += struct.pack(endianess + 'L', 0x00000000)
            else:
                # Add the simple checksum to the Load RCW command
                checksum = 0
                for i in range(0, len(binary), 4):
                    word = struct.unpack(endianess + 'L', binary[i:i+4])[0]
                    checksum += word;
                checksum = checksum & 0xffffffff
                binary += struct.pack(endianess + 'L', checksum)
        
    # Add any PBI commands
    binary += pbi

    # Add the end-command
    if options.pbl:
        if nocrc == 1:
            binary += struct.pack(endianess + 'L', 0x80ff0000)
            binary += struct.pack(endianess + 'L', 0x00000000)
        else:
            if pbiformat == 2:
                crcbinary = pbi

                # CRC and Stop
                cmd = struct.pack(endianess + 'L', 0x808f0000)
                invert = 0xffffffff
            else:
                crcbinary = binary
                cmd = struct.pack(endianess + 'L', 0x08000040 | (int(vars['pbladdr'], 16) & 0x00ffff00))
                invert = 0

            crcbinary += cmd

            # Precise bit any byte ordering of the CRC calculation is
            # not clearly specified. This is empirical.
            if classicbitnumbers:
                    newcrcbinary = b''
                    for c in crcbinary:
                        byte = int(c)
                        byte = int(bin(byte)[2:].zfill(8)[::-1], 2)
                        newcrcbinary += bytes([byte])
                    crcbinary = newcrcbinary

            # Calculate and add the CRC
            crc = crc32(crcbinary) & 0xffffffff

            if classicbitnumbers:
                    crc = int(bin(crc)[2:].zfill(32)[::-1], 2)

            crc ^= invert
            binary += cmd
            binary += struct.pack(endianess + 'L', crc)

    if endianess64b:
        l = len(binary)
        if dont64bswapcrc and options.pbl:
            l -= 8
        newbinary = b''
        for i in range(0, l, 8):
                x64 = struct.unpack('>Q', binary[i:i + 8])[0]
                newbinary += struct.pack('<Q', x64)
        if l < len(binary):
                newbinary += binary[i+8:i+16]
        binary = newbinary

    return binary

def _get_var_int(name, default=0):
    """Return vars[name] as int, or default if absent."""
    return int(vars[name], 0) if name in vars else default

def _resolve_endianess():
    """Return (endianess, endianessrev) struct format characters."""
    if _get_var_int('littleendian'):
        return '<', '>'
    return '>', '<'

def _unswap64b(binary, size_bytes, dont64bswapcrc):
    """Re-sort 64-bit quads for littleendian64b images."""
    binary = binary[:len(binary) & ~7]
    l = len(binary)
    if dont64bswapcrc and l > size_bytes:
        l -= 8
    result = b''
    for i in range(0, l, 8):
        x64 = struct.unpack('>Q', binary[i:i+8])[0]
        result += struct.pack('<Q', x64)
    if l < len(binary):
        result += binary[l:l+8]
    return result

def _split_binary(binary, size, pbiformat, endianess):
    """
    Strip PBL preamble/checksum from binary.
    Returns (bitbytes, pbi_bytes).
    bitbytes: the raw RCW field bytes.
    pbi_bytes: the raw PBI command stream (b'' if absent).
    """
    rcw_len = size // 8
    if len(binary) <= rcw_len:
        return binary, b''

    preamble = struct.pack(endianess + 'L', 0xaa55aa55)
    if binary[:4] != preamble:
        print('Weird binary RCW format!')
        return b'', b''

    rcw_start = 8
    rcw_end   = rcw_start + rcw_len
    bitbytes  = binary[rcw_start:rcw_end]

    # pbiformat==2 has an extra checksum word after the RCW
    pbi_start = rcw_end + 4 if pbiformat == 2 else rcw_end
    return bitbytes, binary[pbi_start:]

def _bitbytes_to_int(bitbytes, classicbitnumbers):
    """
    Convert the raw RCW byte string to a single wide integer 'bits',
    with LSB on the right, honouring classicbitnumbers bit-reversal.
    """
    if classicbitnumbers:
        bitbytes = bytes(
            int(bin(b)[2:].zfill(8)[::-1], 2) for b in bitbytes
        )
        bitstring = ''.join('{0:08b}'.format(b) for b in bitbytes)[::-1]
    else:
        bitstring = ''.join('{0:08b}'.format(b) for b in bitbytes)[::-1]
    return int(bitstring, 2)

def _decode_rcw_fields(bits):
    """
    Walk all known symbols, extract each field value from 'bits', and
    return (source_lines, residual_bits) where residual_bits holds any
    set bits not covered by a known symbol.
    """
    lines = ''
    for n, [bb, ee] in symbols.ordered_items():
        b, e = min(bb, ee), max(bb, ee)
        s    = 1 + e - b
        mask = (1 << s) - 1
        v    = (bits >> b) & mask
        # PPC-style bit numbering: reverse the extracted field value
        if b == bb:
            v = int(bin(v)[2:].zfill(s)[::-1], 2)
        if v:
            lines += ('%s=0x%x\n' if s > 8 else '%s=%u\n') % (n, v)
            bits &= ~(mask << b)
    return lines, bits

def _warn_unknown_bits(bits):
    """Print positions of any bits still set after field extraction."""
    if not bits:
        return
    print('Unknown bits in positions:')
    n, mask = 0, 1
    while bits:
        if bits & mask:
            print(n)
        n    += 1
        bits &= ~mask
        mask <<= 1

def _read_words(pbi, i, count, endianess):
    """Read 'count' consecutive 32-bit words from pbi[i:]; return (list, new_i)."""
    words = []
    for _ in range(count):
        words.append(struct.unpack(endianess + 'L', pbi[i:i+4])[0])
        i += 4
    return words, i

def _disasm_pbi_gen3_general(word, pbi, i, endianess):
    """
    Disassemble one pbiformat==2 general command (hdr == 0x80).
    Returns (source_line, new_i).
    """
    cmd = (word >> 16) & 0xff

    # Block Copy (RM §8.3.3)
    if cmd == 0x00:
        (a1, a2, a3), i = _read_words(pbi, i, 3, endianess)
        return "blockcopy 0x%02x,0x%08x,0x%08x,0x%08x\n" % (word & 0xff, a1, a2, a3), i
    # Load RCW / Load Sec Hdr / Load Boot1 CSF
    if cmd in (0x10, 0x11, 0x20, 0x22):
        return "/* Disassemble not implemented for word 0x%08x */\n" % word, i
    # Load AltCfg Window (RM §8.3.6)
    if cmd == 0x12:
        return "loadacwindow 0x%08x\n" % (word & 0x3fff), i
    # Load Condition (RM §8.3.7)
    if cmd == 0x14:
        (a1, a2), i = _read_words(pbi, i, 2, endianess)
        return "loadc 0x%08x,0x%08x\n" % (a1, a2), i
    # Poll Short / Poll Long (RM §8.3.11)
    if cmd in (0x80, 0x81):
        tag = 'short' if cmd == 0x80 else 'long'
        (a1, a2, a3), i = _read_words(pbi, i, 3, endianess)
        return "poll.%s 0x%08x,0x%08x,0x%08x\n" % (tag, a1, a2, a3), i
    # Wait (RM §8.3.12)
    if cmd == 0x82:
        return "wait 0x%08x\n" % (word & 0xffff), i
    # Jump (RM §8.3.13)
    if cmd == 0x84:
        (a1,), i = _read_words(pbi, i, 1, endianess)
        return "jump 0x%08x\n" % a1, i
    # Jump Conditional (RM §8.3.14)
    if cmd == 0x85:
        (a1, a2), i = _read_words(pbi, i, 2, endianess)
        return "jumpc 0x%08x,0x%08x\n" % (a1, a2), i
    # CRC and Stop (RM §8.3.15)
    if cmd == 0x8f:
        (a1,), i = _read_words(pbi, i, 1, endianess)
        return "/* CRC and Stop command (CRC 0x%08x)*/\n" % a1, i
    # Stop (RM §8.3.16)
    if cmd == 0xff:
        i += 4
        return "/* Stop command */\n", i

    return "/* Unknown word 0x%08x */\n" % word, i

def _disasm_pbi_gen3_ccsr_write(word, pbi, i, endianess):
    """
    Disassemble one pbiformat==2 CCSR Write command (hdr & 0xc0 == 0x00).
    B field (bits 29:28) selects byte count: B=1 -> write.b1, B=3 -> write.
    B=2 is illegal per RM Table 33 and falls through to unknown-word comment.
    Returns (source_line, new_i).
    """
    b_field = (word >> 28) & 0x3
    (a1,), i = _read_words(pbi, i, 1, endianess)
    if b_field == 0x1:
        return "write.b1 0x%08x,0x%08x\n" % (word & 0x0fffffff, a1), i
    if b_field == 0x3:
        return "write 0x%08x,0x%08x\n"    % (word & 0x0fffffff, a1), i
    return "/* Unknown word 0x%08x */\n" % word, i

def _disasm_pbi_gen3_altcfg_write(word, pbi, i, endianess):
    """
    Disassemble one pbiformat==2 AltConfig Write command (hdr & 0xc0 == 0x80).
    The B field (bits 29:26) encodes the byte count N = 2^(B-1).
    Returns (source_line, new_i).
    """
    b_field = (word >> 26) & 0xf
    if not b_field:
        return "/* Unknown word 0x%08x */\n" % word, i
    line = "awrite 0x%08x" % (word & 0x03ffffff)
    for _ in range(0, 1 << (b_field - 1), 4):
        (a1,), i = _read_words(pbi, i, 1, endianess)
        line += ",0x%08x" % a1
    return line + "\n", i

def _disasm_pbi_gen3(pbi_bytes, endianess):
    """
    Disassemble a pbiformat==2 PBI command stream.
    Returns the source text for the .pbi block body (without .pbi/.end markers).
    """
    # Pad to the next 4-byte boundary (0 bytes if already aligned).
    pbi = pbi_bytes + bytearray(-len(pbi_bytes) % 4)
    l = len(pbi)
    lines = ''
    i = 0

    while i < l:
        word = struct.unpack(endianess + 'L', pbi[i:i+4])[0]
        i += 4
        hdr = (word >> 24) & 0xff
        if hdr == 0x80:
            line, i = _disasm_pbi_gen3_general(word, pbi, i, endianess)
        elif (hdr & 0xc0) == 0x00:
            line, i = _disasm_pbi_gen3_ccsr_write(word, pbi, i, endianess)
        elif (hdr & 0xc0) == 0x80:
            line, i = _disasm_pbi_gen3_altcfg_write(word, pbi, i, endianess)
        else:
            line = "/* Unknown word 0x%08x */\n" % word
        lines += line
    return lines

def _disasm_pbi_legacy(pbi_bytes, endianess, endianessrev):
    """
    Disassemble a legacy (pbiformat != 2) PBI command stream.
    Returns the source text for the .pbi block body (without .pbi/.end markers).
    """
    # Pad to the next 4-byte boundary (0 bytes if already aligned).
    pbi = pbi_bytes + bytearray(-len(pbi_bytes) % 4)
    l = len(pbi)
    pbladdr = int(vars['pbladdr'], 16) & 0x00ffff00
    crcstopcheck = 0x08000040 | pbladdr
    le64b = _get_var_int('littleendian64b')
    lines = ''
    i = 0

    while i < l:
        word = struct.unpack(endianess + 'L', pbi[i:i+4])[0]
        i += 4
        hdr = (word >> 24) & 0xff

        # Magic hack to overcome broken binary PBI entries shipping in the SDK for LS1
        if (le64b and i + 4 == l and
                struct.unpack(endianessrev + 'L', pbi[i:i+4])[0] == crcstopcheck):
            lines += "/* CRC and Stop command (CRC 0x%08x)*/\n" % word
            i += 4
            continue

        if (hdr & 0x01) == 0x01:
            addr = word & 0x00ffffff
            cnt  = (hdr >> 1) & 0x3f or 64
            if i + cnt >= l:
                print('Error in write 0x%08x at offset %d within PBI\n' % (word, i))
            if (addr & 0x00ffff00) == pbladdr:
                (a1,), i = _read_words(pbi, i, 1, endianess)
                lo = addr & 0xff
                if   lo == 0x00: lines += "flush\n"
                elif lo == 0x40: lines += "/* CRC command (CRC 0x%08x)*/\n" % a1
                elif lo == 0x80: lines += "jump 0x%08x\n" % a1
                elif lo == 0xc0: lines += "wait %u\n" % a1
            else:
                prefix = 'a' if (hdr & 0x80) else ''
                line   = "%swrite 0x%08x" % (prefix, addr)
                for _ in range(0, cnt, 4):
                    (a1,), i = _read_words(pbi, i, 1, endianess)
                    line += ",0x%08x" % a1
                lines += line + "\n"
        elif (hdr & 0x81) == 0x00:
            (a1,), i = _read_words(pbi, i, 1, endianess)
            lines += "/* CRC and Stop command (CRC 0x%08x)*/\n" % a1
        else:
            lines += "/* Unknown word 0x%08x */\n" % word
    return lines

# Create a source file from a binary and a .rcwi file
def create_source():
    global symbols
    global vars
    global options

    # Read raw binary
    with open(options.input, 'rb') as f:
        binary = f.read()

    # Resolve configuration
    size = int(vars['size'], 0)
    pbiformat = _get_var_int('pbiformat')
    classicbitnumbers = _get_var_int('classicbitnumbers')
    endianess, endianessrev = _resolve_endianess()
    dont64bswapcrc = _get_var_int('dont64bswapcrc')

    # Undo 64-bit quad byte-swap if required
    if _get_var_int('littleendian64b'):
        binary = _unswap64b(binary, size // 8, dont64bswapcrc)

    # Emit header include
    source = '#include <%s>\n\n' % os.path.basename(options.rcwi)

    # Split preamble -> RCW bitbytes + PBI stream
    bitbytes, pbi_bytes = _split_binary(binary, size, pbiformat, endianess)

    # Decode RCW bitfields
    bits = _bitbytes_to_int(bitbytes, classicbitnumbers)
    field_lines, bits = _decode_rcw_fields(bits)
    source += field_lines
    _warn_unknown_bits(bits)

    # Disassemble PBI command stream
    if len(pbi_bytes) > 0:
        if pbiformat == 2:
            body = _disasm_pbi_gen3(pbi_bytes, endianess)
        else:
            body = _disasm_pbi_legacy(pbi_bytes, endianess, endianessrev)
        source += "\n.pbi\n" + body + ".end\n"

    return source

if (sys.version_info < (3,0)):
    print('Only Python versions 3.0+ are supported.')
    sys.exit(1)

# Make all 'print' statements output to stderr instead of stdout
sys.stdout = sys.stderr

command_line()

symbols = {}
assignments = {}
vars = {'pbladdr':'0x138000'}
pbi = ''

if options.reverse:
    source = read_source_file(options.rcwi)
    if not source:
        sys.exit(1)
    parse_source_file(source)

    f = open(options.output, 'w')
    f.write(create_source())
    f.close()
else:
    source = read_source_file(options.input)
    if not source:
        sys.exit(1)
    parse_source_file(source)
    check_vars()
    parse_cmdline_bitfields()

    # Write it all to the output file
    f = open(options.output, 'wb')
    f.write(create_binary())
    f.close()
