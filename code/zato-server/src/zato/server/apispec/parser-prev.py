# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from inspect import getmodule

# docformatter
from docformatter import format_docstring

# Zato
from zato.common.api import APISPEC
from zato.common.marshal_.api import Model
from zato.common.marshal_.simpleio import DataClassSimpleIO
from zato.server.apispec.model import APISpecInfo, Config, Docstring, FieldInfo, Namespace, SimpleIO, SimpleIODescription

# Zato - Cython
from zato.simpleio import SIO_TYPE_MAP

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from dataclasses import Field
    from zato.common.typing_ import any_, anydict, anylist, anytuple, iterator_, list_, optional, strlist, \
        strorlist, type_
    from zato.server.service import Service

    Field   = Field
    Service = Service

# ################################################################################################################################
# ################################################################################################################################

_SIO_TYPE_MAP = SIO_TYPE_MAP()

# ################################################################################################################################
# ################################################################################################################################

tag_internal = ('@classified', '@confidential', '@internal', '@private', '@restricted', '@secret')
tag_html_internal = """
.. raw:: html

    <span class="zato-tag-name-highlight">{}</span>
"""

not_public = 'INFORMATION IN THIS SECTION IS NOT PUBLIC'

# ################################################################################################################################
# ################################################################################################################################

def build_field_list(model:'Model', api_spec_info:'any_') -> 'anylist':

    # Response to produce
    out = []

    # All the fields of this dataclass
    python_field_list = model.zato_get_fields()

    for _, field in sorted(python_field_list.items()): # type: (str, Field)

        # Parameter details object
        info = FieldInfo.from_python_field(field, api_spec_info)
        out.append(info)

    return out

# ################################################################################################################################
# ################################################################################################################################

class _DocstringSegment:
    __slots__ = 'tag', 'summary', 'description', 'full'

    def __init__(self) -> 'None':
        self.tag = ''         # type: str
        self.summary = ''     # type: str
        self.description = '' # type: str
        self.full = ''        # type: str

# ################################################################################################################################

    def to_dict(self) -> 'anydict':
        return {
            'tag': self.tag,
            'summary': self.summary,
            'description': self.description,
            'full': self.full,
        }

# ################################################################################################################################
# ################################################################################################################################

class ServiceInfo:
    """ Contains information about a service basing on which documentation is generated.
    """
    def __init__(
        self,
        name,               # type: str
        service_class,      # type: type_[Service]
        simple_io_config,   # type: anydict
        tags='public',      # type: strorlist
        needs_sio_desc=True # type: bool
        ) -> 'None':
        self.name = name
        self.service_class = service_class
        self.simple_io_config = simple_io_config
        self.config = Config()
        self.simple_io = {} # type: anydict
        self.docstring = Docstring(tags if isinstance(tags, list) else [tags])

        self.namespace = Namespace()
        self.needs_sio_desc = needs_sio_desc

        self.run()

# ################################################################################################################################

    def run(self) -> 'None':
        self.parse_simple_io()
        self.parse_docstring()

# ################################################################################################################################

    def parse_simple_io(self) -> 'None':
        """ Adds metadata about the service's namespace and SimpleIO definition.
        """
        # Namespace can be declared as a service-level attribute of a module-level one. Former takes precedence.
        service_ns = getattr(self.service_class, 'namespace', APISPEC.NAMESPACE_NULL)
        mod = getmodule(self.service_class)
        mod_ns = getattr(mod, 'namespace', APISPEC.NAMESPACE_NULL)

        self.namespace.name = service_ns if service_ns else mod_ns

        # Set namespace's documentation but only if it was declared top-level and is equal to our own
        if self.namespace.name and self.namespace.name == mod_ns:
            self.namespace.docs = getattr(mod, 'namespace_docs', '')

        # SimpleIO
        sio = getattr(self.service_class, '_sio', None) # type: optional[DataClassSimpleIO]

        if sio:

            # This can be reused across all the output data formats
            sio_desc = self.get_sio_desc(sio)

            for api_spec_info in _SIO_TYPE_MAP:

                _api_spec_info = APISpecInfo()
                _api_spec_info.name = api_spec_info.name
                _api_spec_info.field_list = {}
                _api_spec_info.request_elem = getattr(sio, 'request_elem', '')
                _api_spec_info.response_elem = getattr(sio, 'response_elem', '')

                for sio_attr_name in ('input', 'output'): # type: str
                    model = getattr(sio.user_declaration, sio_attr_name, None) # type: optional[Model]
                    if model:
                        _api_spec_info.field_list[sio_attr_name] = build_field_list(model, api_spec_info)

                self.simple_io[_api_spec_info.name] = SimpleIO(_api_spec_info, sio_desc, self.needs_sio_desc).to_bunch()

# ################################################################################################################################

    def _parse_split_segment(
        self,
        tag,             # type: str
        split,           # type: anylist
        is_tag_internal, # type: bool
        prefix_with_tag  # type: bool
        ) -> '_DocstringSegment':

        if is_tag_internal:
            split.insert(0, not_public)

        # For implicit tags (e.g. public), the summary will be under index 0,
        # but for tags named explicitly, index 0 may be an empty element
        # and the summary will be under index 1.
        summary = split[0] or split[1]

        # format_docstring expects an empty line between summary and description
        if len(split) > 1:
            _doc = []
            _doc.append(split[0])
            _doc.append('')
            _doc.extend(split[1:])
            doc = '\n'.join(_doc)
        else:
            doc = ''

        # This gives us the full docstring out of which we need to extract description alone.
        full_docstring = format_docstring('', '"{}"'.format(doc), post_description_blank=False)
        full_docstring = full_docstring.lstrip('"').rstrip('"')
        description = full_docstring.splitlines()

        # If there are multiple lines and the second one is empty this means it is an indicator of a summary to follow.
        if len(description) > 1 and not description[1]:
            description = '\n'.join(description[2:])
        else:
            description = ''

        # Function docformatter.normalize_summary adds a superfluous period at the end of docstring.
        if full_docstring:
            if description and full_docstring[-1] == '.' and full_docstring[-1] != description[-1]:
                full_docstring = full_docstring[:-1]

            if summary and full_docstring[-1] == '.' and full_docstring[-1] != summary[-1]:
                full_docstring = full_docstring[:-1]

        # If we don't have any summary but there is a docstring at all then it must be a single-line one
        # and it becomes our summary.
        if full_docstring and not summary:
            summary = full_docstring

        # If we don't have description but we have summary then summary becomes description and full docstring as well
        if summary and not description:
            description = summary
            full_docstring = summary

        summary = summary.lstrip()

        # This is needed in case we have one of the tags
        # that need a highlight because they contain information
        # that is internal to users generating the specification.
        tag_html = tag

        if is_tag_internal:
            tag_html = tag_html_internal.format(tag)
        else:
            tag_html = tag

        if prefix_with_tag:
            description = '\n\n{}\n{}'.format(tag_html, description)
            full_docstring = '\n{}\n\n{}'.format(tag_html, full_docstring)

        out = _DocstringSegment()
        out.tag = tag.replace('@', '', 1)
        out.summary = summary
        out.description = description
        out.full = full_docstring

        return out

# ################################################################################################################################

    def _get_next_split_segment(self, lines:'anylist', tag_indicator:'str'='@') -> 'iterator_[anytuple]':

        current_lines = [] # type: strlist
        len_lines = len(lines) -1 # type: int # Substract one because enumerate counts from zero

        # The very first line must contain tag name(s),
        # otherwise we assume that it is the implicit name, called 'public'.
        first_line = lines[0] # type: str
        current_tag = first_line.strip().replace(tag_indicator, '', 1) if \
            first_line.startswith(tag_indicator) else APISPEC.DEFAULT_TAG # type: str

        # Indicates that we are currently processing the very first line,
        # which is needed because if it starts with a tag name
        # then we do not want to immediately yield to our caller.
        in_first_line = True

        for idx, line in enumerate(lines): # type: (int, str)

            line_stripped = line.strip()
            if line_stripped.startswith(tag_indicator):
                if not in_first_line:
                    yield current_tag, current_lines
                    current_tag = line_stripped
                    current_lines[:] = []
            else:
                in_first_line = False
                current_lines.append(line)
                if idx == len_lines:
                    yield current_tag, current_lines
                    break
        else:
            yield current_tag, current_lines

# ################################################################################################################################

    def extract_segments(self, doc:'str') -> 'list_[_DocstringSegment]':
        """ Makes a pass over the docstring to extract all of its tags and their text.
        """
        # Response to produce
        out = [] # type: list_[_DocstringSegment]

        # Nothing to parse
        if not doc:
            return out

        # All lines in the docstring, possibly containing multiple tags
        all_lines = doc.strip().splitlines() # type: anylist

        # Again, nothing to parse
        if not all_lines:
            return out

        # Contains all lines still to be processed - function self._get_next_split_segment will update it in place.
        current_lines = all_lines[:]

        for tag, tag_lines in self._get_next_split_segment(current_lines):

            # All non-public tags are shown explicitly
            prefix_with_tag = tag != 'public'

            # A flag indicating whether we are processing a public or an internal tag,
            # e.g. public vs. @internal or @confidential.
            for name in tag_internal:
                if name in tag:
                    is_tag_internal = True
                    break
            else:
                is_tag_internal = False

            segment = self._parse_split_segment(tag, tag_lines, is_tag_internal, prefix_with_tag)

            if segment.tag in self.docstring.tags:
                out.append(segment)

        return out

# ################################################################################################################################

    def parse_docstring(self) -> 'None':

        segments = self.extract_segments(self.service_class.__doc__ or '')

        for segment in segments: # type: _DocstringSegment

            # The very first summary found will set the whole docstring's summary
            if segment.summary:
                if not self.docstring.summary:
                    self.docstring.summary = segment.summary

            if segment.description:
                self.docstring.description += segment.description

            if segment.full:
                self.docstring.full += segment.full

# ################################################################################################################################

    def get_sio_desc(self, sio:'any_', io_separator:'str'='/', new_elem_marker:'str'='*') -> 'SimpleIODescription':

        out = SimpleIODescription()
        doc = sio.service_class.SimpleIO.__doc__

        # No description to parse
        if not doc:
            return out

        doc = doc.strip() # type: str # type: ignore[no-redef]

        lines = [] # type: strlist

        # Strip leading whitespace but only from lines containing element names
        for line in doc.splitlines(): # type: str
            orig_line = line
            line = line.lstrip()
            if line.startswith(new_elem_marker):
                lines.append(line)
            else:
                lines.append(orig_line)

        # Now, replace all the leading whitespace left with endline characters,
        # but instead of replacing them in place, they will be appending to the preceding line.

        # This will contain all lines with whitespace replaced with newlines
        with_new_lines = []

        for idx, line in enumerate(lines):

            # By default, assume that do not need to append the new line
            append_new_line = False

            # An empty line is interpreted as a new line marker
            if not line:
                append_new_line = True

            if line.startswith(' '):
                line = line.lstrip()

            with_new_lines.append(line)

            # Alright, this line started with whitespace which we removed above,
            # so now we need to append the new line character. But first we need to
            # find an index of any previous line that is not empty in case there
            # are multiple empty lines in succession in the input string.
            if append_new_line:

                line_found = False
                line_idx = idx

                while not line_found or (idx == 0):
                    line_idx -= 1
                    current_line = with_new_lines[line_idx]
                    if current_line.strip():
                        break

                with_new_lines[line_idx] += '\n'

        # We may still have some empty lines left over which we remove now
        lines = [elem for elem in with_new_lines[:] if elem]

        input_lines  = [] # type: strlist
        output_lines = [] # type: strlist

        # If there is no empty line, the docstring will describe either input or output (we do not know yet).
        # If there is only one empty line, it constitutes a separator between input and output.
        # If there is more than one empty line, we need to look up the separator marker instead.
        # If the separator is not found, it again means that the docstring describes either input or output.
        empty_line_count = 0

        # Line that separates input from output in the list of arguments
        input_output_sep_idx = 0 # Initially empty, i.e. set to zero

        # To indicate whether we have found a separator in the docstring
        has_separator = False

        for idx, line in enumerate(lines):
            if not line:
                empty_line_count += 1
                input_output_sep_idx = idx

            if line == io_separator:
                has_separator = True
                input_output_sep_idx = idx

        # No empty line separator = we do not know if it is input or output so we need to populate both structures ..
        if empty_line_count == 0:
            input_lines[:] = lines[:]
            output_lines[:] = lines[:]

        # .. a single empty line separator = we know where input and output are.
        elif empty_line_count == 1:
            input_lines[:] = lines[:input_output_sep_idx]
            output_lines[:] = lines[input_output_sep_idx+1:]

        else:
            # If we have a separator, this is what indicates where input and output are ..
            if has_separator:
                input_lines[:] = lines[:input_output_sep_idx-1]
                output_lines[:] = lines[input_output_sep_idx-1:]

            # .. otherwise, we treat it as a list of arguments and we do not know if it is input or output.
            else:
                input_lines[:] = lines[:]
                output_lines[:] = lines[:]

        input_lines = [elem for elem in input_lines if elem and elem != io_separator]
        output_lines = [elem for elem in output_lines if elem and elem != io_separator]

        out.input.update(self._parse_sio_desc_lines(input_lines))
        out.output.update(self._parse_sio_desc_lines(output_lines))

        return out

# ################################################################################################################################

    def _parse_sio_desc_lines(self, lines:'anylist', new_elem_marker:'str'='*') -> 'anydict':
        out = {}
        current_elem = None

        for line in lines: # type: str
            if line.startswith(new_elem_marker):

                # We will need it later below
                orig_line = line

                # Remove whitespace, skip the new element marker and the first string left over will be the element name.
                line_list = [elem for elem in line.split()] # type: strlist
                line_list.remove(new_elem_marker)
                current_elem = line_list[0]

                # We have the element name so we can now remove it from the full line
                to_remove = '{} {} - '.format(new_elem_marker, current_elem)
                after_removal = orig_line.replace(to_remove, '', 1)
                out[current_elem] = [after_removal]

            else:
                if current_elem:
                    out[current_elem].append(line)

        # Joing all the lines into a single string, preprocessing them along the way.
        for key, value in out.items():

            # We need to strip the trailing new line characters from the last element  in the list of lines
            # because it is redundant and our callers would not want to render it anyway.
            last = value[-1]
            last = last.rstrip()
            value[-1] = last

            # Joing the lines now, honouring new line characters. Also, append whitespace
            # but only to elements that are not the last in the list because they end a sentence.
            new_value = []
            len_value = len(value)
            for idx, elem in enumerate(value, 1): # type: (int, str)
                if idx != len_value and not elem.endswith('\n'):
                    elem += ' '
                new_value.append(elem)

            # Everything is preprocesses so we can create a new string now ..
            new_value = ''.join(new_value) # type: ignore[assignment]

            # .. and set it for that key.
            out[key] = new_value

        return out

# ################################################################################################################################
# ################################################################################################################################
