############################################################################
# scspell
# Copyright (C) 2009 Paul Pelzl
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
############################################################################


"""
scspell -- an interactive, conservative spell-checker for source code.
"""


from __future__ import with_statement
import os, re, sys, shutil
from bisect import bisect_left
import ConfigParser

import _portable
from _corpus import CorporaFile
from _util import *


VERSION = '0.1.0'
CONTEXT_SIZE  = 4       # Size of context printed upon request
LEN_THRESHOLD = 3       # Subtokens shorter than 4 characters are likely to be abbreviations
CTRL_C = '\x03'         # Special key codes returned from getch()
CTRL_D = '\x04'
CTRL_Z = '\x1a'

USER_DATA_DIR        = _portable.get_data_dir('scspell')
KEYWORDS_DEFAULT_LOC = os.path.join(USER_DATA_DIR, 'keywords.txt')
SCSPELL_DATA_DIR     = os.path.normpath(os.path.join(os.path.dirname(__file__), 'data'))
SCSPELL_CONF         = os.path.join(USER_DATA_DIR, 'scspell.conf')

# Treat anything alphanumeric as a token of interest
token_regex = re.compile(r'\w+')

# Hex digits will be treated as a special case, because they can look like
# word-like even though they are actually numeric
hex_regex = re.compile(r'0x[0-9a-fA-F]+')

# We assume that tokens will be split using either underscores,
# digits, or camelCase conventions (or both)
us_regex         = re.compile(r'[_\d]+')
camel_word_regex = re.compile(r'([A-Z][a-z]*)')


class MatchDescriptor(object):
    """A MatchDescriptor captures the information necessary to represent a token
    matched within some source code.
    """

    def __init__(self, text, matchobj):
        self._data     = text
        self._pos      = matchobj.start()
        self._token    = matchobj.group()
        self._context  = None
        self._line_num = None

    def get_token(self):
        return self._token

    def get_string(self):
        """Get the entire string in which the match was found."""
        return self._data

    def get_ofs(self):
        """Get the offset within the string where the match is located."""
        return self._pos

    def get_prefix(self):
        """Get the string preceding this match."""
        return self._data[:self._pos]

    def get_remainder(self):
        """Get the string consisting of this match and all remaining characters."""
        return self._data[self._pos:]

    def get_context(self):
        """Compute the lines of context associated with this match, as a sequence of
        (line_num, line_string) pairs.
        """
        if self._context is not None:
            return self._context

        lines = self._data.split('\n')

        # Compute the byte offset of start of every line
        offsets = []
        for i in xrange(len(lines)):
            if i == 0:
                offsets.append(0)
            else:
                offsets.append(offsets[i-1] + len(lines[i-1]) + 1)

        # Compute the line number where the match is located
        for (i, ofs) in enumerate(offsets):
            if ofs > self._pos:
                self._line_num = i
                break
        if self._line_num is None:
            self._line_num = len(lines)

        # Compute the set of lines surrounding this line number
        self._context = [(i+1, line.strip('\r\n')) for (i, line) in enumerate(lines) if 
                (i+1 - self._line_num) in range(-CONTEXT_SIZE/2, CONTEXT_SIZE/2 + 1)]
        return self._context

    def get_line_num(self):
        """Computes the line number of the match."""
        if self._line_num is None:
            self.get_context()
        return self._line_num


def make_unique(items):
    """Remove duplicate items from a list, while preserving list order."""
    seen = set()
    def first_occurrence(i):
        if i not in seen:
            seen.add(i)
            return True
        return False
    return [i for i in items if first_occurrence(i)]


def decompose_token(token):
    """Divide a token into a list of strings of letters.

    Tokens are divided by underscores and digits, and capital letters will begin
    new subtokens.

    :param token: string to be divided
    :returns: sequence of subtoken strings
    """
    us_parts = us_regex.split(token)
    if ''.join(us_parts).isupper():
        # This looks like a CONSTANT_DEFINE_OF_SOME_SORT
        subtokens = us_parts
    else:
        camelcase_parts = [camel_word_regex.split(us_part) for us_part in us_parts]
        subtokens = sum(camelcase_parts, [])
    # This use of split() will create many empty strings
    return [st.lower() for st in subtokens if st != '']
    

def handle_add(unmatched_subtokens, filename, dicts):
    """Handle addition of one or more subtokens to a dictionary."""
    for subtoken in unmatched_subtokens:
        while True:
            print ("""\
   Subtoken '%s':
      (i)gnore, add to (p)rogramming language dictionary, or add to (n)atural language
      dictionary? [i]""") % subtoken
            ch = _portable.getch()
            if ch in (CTRL_C, CTRL_D, CTRL_Z):
                print 'User abort.'
                sys.exit(1)
            elif ch in ('i', '\r', '\n'):
                break
            elif ch == 'p':
                dicts.add_filetype(subtoken, filename)
                break
            elif ch == 'n':
                dicts.add_natural(subtoken)
                break


def handle_failed_check(match_desc, filename, unmatched_subtokens, dicts, ignores):
    """Handle a token which failed the spell check operation.

    :param match_desc: description of the token matching instance
    :type  match_desc: MatchDescriptor
    :param filename: name of file containing the token
    :param unmatched_subtokens: sequence of subtokens, each of which failed spell check
    :param dicts: dictionary set against which to perform matching
    :type  dicts: CorporaFile
    :param ignores: set of tokens to ignore for this session
    :returns: (text, ofs) where ``text`` is the (possibly modified) source contents and
            ``ofs`` is the byte offset within the text where searching shall resume.
    """
    token = match_desc.get_token()
    print "%s:%u: Unmatched '%s' --> {%s}" % (filename, match_desc.get_line_num(), token, 
                ', '.join([st for st in unmatched_subtokens]))
    match_regex = re.compile(re.escape(match_desc.get_token()))
    while True:
        print """\
   (i)gnore, (I)gnore all, (r)eplace, (R)eplace all, (a)dd to dictionary, or show (c)ontext? [i]"""
        ch = _portable.getch()
        if ch in (CTRL_C, CTRL_D, CTRL_Z):
            print 'User abort.'
            sys.exit(1)
        elif ch in ('i', '\r', '\n'):
            break
        elif ch == 'I':
            ignores.add(token.lower())
            break
        elif ch == 'r':
            replacement = raw_input('      Replacement text: ')
            if replacement == '':
                print '      (Not replaced.)'
                break
            ignores.add(replacement.lower())
            tail = re.sub(match_regex, replacement, match_desc.get_remainder(), 1)
            return (match_desc.get_prefix() + tail, match_desc.get_ofs() + len(replacement))
        elif ch == 'R':
            replacement = raw_input('      Replacement text: ')
            if replacement == '':
                print '      (Not replaced.)'
                break
            ignores.add(replacement.lower())
            tail = re.sub(match_regex, replacement, match_desc.get_remainder())
            return (match_desc.get_prefix() + tail, match_desc.get_ofs() + len(replacement))
        elif ch == 'a':
            handle_add(unmatched_subtokens, filename, dicts)
            break
        elif ch == 'c':
            for ctx in match_desc.get_context():
                print '%4u: %s' % ctx
            print
    print
    # Default: text is unchanged
    return (match_desc.get_string(), match_desc.get_ofs() + len(match_desc.get_token()))


def spell_check_token(match_desc, filename, dicts, ignores):
    """Spell check a single token.

    :param match_desc: description of the token matching instance
    :type  match_desc: MatchDescriptor
    :param filename: name of file containing the token
    :param dicts: dictionary set against which to perform matching
    :type  dicts: CorporaFile
    :param ignores: set of tokens to ignore for this session
    :returns: (text, ofs) where ``text`` is the (possibly modified) source contents and
            ``ofs`` is the byte offset within the text where searching shall resume.
    """
    token = match_desc.get_token()
    if (token.lower not in ignores) and (hex_regex.match(token) is None):
        subtokens = decompose_token(token)
        unmatched_subtokens = [st for st in subtokens if len(st) > LEN_THRESHOLD
                                                   and (not dicts.match(token, filename))
                                                   and (st not in ignores)]
        if unmatched_subtokens != []:
            unmatched_subtokens = make_unique(unmatched_subtokens)
            return handle_failed_check(match_desc, filename, unmatched_subtokens, dicts, ignores)
    return (match_desc.get_string(), match_desc.get_ofs() + len(token))


def spell_check_file(filename, dicts, ignores):
    """Spell check a single file.

    :param filename: name of the file to check
    :param dicts: dictionary set against which to perform matching
    :type  dicts: CorporaFile
    :param ignores: set of tokens to ignore for this session
    """
    fq_filename = os.path.normcase(os.path.realpath(filename))
    try:
        with open(fq_filename, 'rb') as source_file:
            source_text = source_file.read()
    except IOError, e:
        print 'Error: can\'t read source file "%s"; skipping.  (Reason: %s)' % \
                    (filename, str(e))
        return

    data = source_text
    pos  = 0
    while True:
        m = token_regex.search(data, pos)
        if m is None:
            break
        (data, pos) = spell_check_token(MatchDescriptor(data, m), filename, dicts, ignores)

    # Write out the source file if it was modified
    if data != source_text:
        with open(fq_filename, 'wb') as source_file:
            try:
                source_file.write(data)
            except IOError, e:
                print str(e)
                return
            

def verify_user_data_dir():
    """Verify that the user data directory is present, or create one
    from scratch.
    """
    if not os.path.exists(USER_DATA_DIR):
        print 'Creating new personal dictionaries in %s .\n' % USER_DATA_DIR
        os.makedirs(USER_DATA_DIR)
        shutil.copyfile(os.path.join(SCSPELL_DATA_DIR, 'keywords.txt'), KEYWORDS_DEFAULT_LOC)


def locate_keyword_dict():
    """Load the location of the keyword dictionary.  This is either
    the default location, or an override specified in 'scspell.conf'.
    """
    verify_user_data_dir()
    try:
        f = open(SCSPELL_CONF, 'r')
    except IOError:
        return KEYWORDS_DEFAULT_LOC

    config = ConfigParser.RawConfigParser()
    try:
        config.readfp(f)
    except ConfigParser.ParsingError, e:
        print str(e)
        sys.exit(1)
    finally:
        f.close()

    try:
        loc = config.get('Locations', 'keyword_dictionary')
        if os.path.isabs(loc):
            return loc
        else:
            print ('Error while parsing "%s": keyword_dictionary must be an absolute path.' %
                    SCSPELL_CONF)
            sys.exit(1)
    except ConfigParser.Error:
        return KEYWORDS_DEFAULT_LOC


def set_keyword_dict(filename):
    """Set the location of the keyword dictionary to the specified filename.

    :returns: None
    """
    if not os.path.isabs(filename):
        print 'Error: keyword dictionary location must be an absolute path.'
        sys.exit(1)

    verify_user_data_dir()
    config = ConfigParser.RawConfigParser()
    try:
        config.read(SCSPELL_CONF)
    except ConfigParser.ParsingError, e:
        print str(e)
        sys.exit(1)

    try:
        config.add_section('Locations')
    except ConfigParser.DuplicateSectionError:
        pass
    config.set('Locations', 'keyword_dictionary', filename)

    with open(SCSPELL_CONF, 'w') as f:
        config.write(f)


def export_keyword_dict(filename):
    """Export the current keyword dictionary to the specified file.

    :returns: None
    """
    shutil.copyfile(locate_keyword_dict(), filename)

    
def spell_check(source_filenames):
    """Run the interactive spell checker on the set of source_filenames.
    
    :returns: None
    """
    DICT_LOC = os.path.join(SCSPELL_DATA_DIR, 'english-words.txt')

    verify_user_data_dir()
    with CorporaFile(DICT_LOC) as dicts:
        ignores = set()
        for f in source_filenames:
            spell_check_file(f, dicts, ignores)


__all__ = [
    'spell_check',
    'set_keyword_dict',
    'export_keyword_dict',
    'set_verbosity',
    'VERSION',
    'VERBOSITY_NORMAL',
    'VERBOSITY_MAX'
]

