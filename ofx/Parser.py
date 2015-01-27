# vim: set fileencoding=utf-8
""" 
Regex-based parser for OFXv1/v2 based on subclasses of ElemenTree from stdlib.
"""

# stdlib imports
import xml.etree.ElementTree as ET
import re


# local imports
from header import OFXHeader
import aggregates
from Response import OFXResponse


class ParseError(SyntaxError):
    """ Exception raised by parsing errors in this module """
    pass


class OFXTree(ET.ElementTree):
    """ 
    OFX parse tree.

    Overrides ElementTree.ElementTree.parse() to validate and strip the
    the OFX header before feeding the body tags to TreeBuilder
    """
    def parse(self, source):
        if not hasattr(source, 'read'):
            source = open(source)
        source = source.read()

        # Validate and strip the OFX header
        source = OFXHeader.strip(source)

        # Then parse tag soup into tree of Elements
        parser = TreeBuilder(element_factory=Element)
        parser.feed(source)
        self._root = parser.close()

    def convert(self, strict=True):
        if not hasattr(self, '_root'):
            raise ValueError('Must first call parse() to have data to convert')
        # OFXResponse performs validation & type conversion
        return OFXResponse(self, strict=strict)


class TreeBuilder(ET.TreeBuilder):
    """ 
    OFX parser.

    Overrides ElementTree.TreeBuilder.feed() with a regex-based parser that
    handles both OFXv1(SGML) and OFXv2(XML).
    """
    # The body of an OFX document consists of a series of tags.
    # Each start tag may be followed by text (if a data-bearing element)
    # and optionally an end tag (not mandatory for OFXv1 syntax).
    regex = re.compile(r"""<(?P<TAG>[A-Z1-9./]+?)>
                            (?P<TEXT>[^<]+)?
                            (</(?P=TAG)>)?
                            """, re.VERBOSE)

    def feed(self, data):
        """
        Iterate through all tags matched by regex.
        For data-bearing leaf "elements", use TreeBuilder's methods to
            push a new Element, process the text data, and end the element.
        For non-data-bearing "aggregate" branches, parse the tag to distinguish
            start/end tag, and push or pop the Element accordingly.
        """
        for match in self.regex.finditer(data):
            tag, text, closeTag = match.groups()
            text = (text or '').strip() # None has no strip() method
            if len(text):
                # OFX "element" (i.e. data-bearing leaf)
                if tag.startswith('/'):
                    msg = "<%s> is a closing tag, but has trailing text: '%s'"\
                            % (tag, text)
                    raise ParseError(msg)
                self.start(tag, {})
                self.data(text)
                # End tags are optional for OFXv1 data elements
                # End them all, whether or not they're explicitly ended
                try:
                    self.end(tag)
                except ParseError as err:
                    err.message += ' </%s>' % tag # FIXME
                    raise ParseError(err.message)
            else:
                # OFX "aggregate" (tagged branch w/ no data)
                if tag.startswith('/'):
                    # aggregate end tag
                    try:
                        self.end(tag[1:])
                    except ParseError as err:
                        err.message += ' </%s>' % tag # FIXME
                        raise ParseError(err.message)
                else:
                    # aggregate start tag
                    self.start(tag, {})
                    # empty aggregates are legal, so handle them
                    if closeTag:
                        # regex captures the entire closing tag
                       assert closeTag.replace(tag, '') == '</>'
                       try:
                           self.end(tag)
                       except ParseError as err:
                           err.message += ' </%s>' % tag # FIXME
                           raise ParseError(err.message)

    def end(self, tag):
        try:
            super(TreeBuilder, self).end(tag)
        except AssertionError as err:
            # HACK: ET.TreeBuilder.end() raises an AssertionError for internal
            # errors generated by ET.TreeBuilder._flush(), but also for ending
            # tag mismatches, which are problems with the data rather than the
            # parser.  We want to pass on the former but handle the latter;
            # however, the only difference is the error message.
            if 'end tag mismatch' in err.message:
                raise ParseError(err.message)
            else:
                raise


class Element(ET.Element):
    """
    Parse tree node.

    Extends ElementTree.Element with a convert() method that converts OFX
    'aggregates' to the ofx.aggregates.Aggregate object model by converting
    them to flat dictionaries keyed by OFX 'element' tag names, whose values
    have been validated and converted to Python types by subclasses of 
    ofx.elements.Element.
    """
    def convert(self, strict=True):
        """ 
        Convert an OFX 'aggregate' to the ofx.aggregates.Aggregate object model
        by converting it to a flat dictionary keyed by OFX 'element' tag names,
        whose values have been validated and converted to Python types 
        by subclasses of ofx.elements.Element.
        """
        # Strip MFASSETCLASS/FIMFASSETCLASS 
        # - lists that will blow up _flatten()
        if self.tag == 'MFINFO':
            # Do all XPath searches before removing nodes from the tree
            #   which seems to mess up the DOM in Python3 and throw an
            #   AttributeError on subsequent searches.
            mfassetclass = self.find('./MFASSETCLASS')
            fimfassetclass = self.find('./FIMFASSETCLASS')

            if mfassetclass is not None:
                # Convert PORTIONs; save for later
                self.mfassetclass = [p.convert() for p in mfassetclass]
                self.remove(mfassetclass)
            if fimfassetclass is not None:
                # Convert FIPORTIONs; save for later
                self.fimfassetclass = [p.convert() for p in fimfassetclass]
                self.remove(fimfassetclass)
                    
        # Convert parsed OFX aggregate into a flat dictionary of its elements
        attributes = self._flatten()

        # Aggregate classes are named after the OFX tags they represent.
        # Use the tag to look up the right aggregate
        AggregateClass = getattr(aggregates, self.tag)

        # See OFX spec section 5.2 for currency handling conventions.
        # Flattening the currency definition leaves only the CURRATE/CURSYM
        # elements, leaving no indication of whether these were sourced from
        # a CURRENCY aggregate or ORIGCURRENCY.  Since this distinction is
        # important to interpreting transactions in foreign correncies, we
        # preserve this information by adding a nonstandard curtype element.
        if issubclass(AggregateClass, aggregates.ORIGCURRENCY):
            currency = self.find('*/CURRENCY')
            origcurrency = self.find('*/ORIGCURRENCY')
            if (currency is not None) and (origcurrency is not None):
                raise ParseError("<%s> may not contain both <CURRENCY> and \
                                 <ORIGCURRENCY>" % self.tag)
            curtype = currency
            if curtype is None:
                 curtype = origcurrency
            if curtype is not None:
                curtype = curtype.tag
            attributes['curtype'] = curtype

        # Feed the flattened dictionary of attributes to the Aggregate
        # subclass for validation and type conversion
        aggregate = AggregateClass(strict=strict, **attributes)

        # Staple MFASSETCLASS/FIMFASSETCLASS onto MFINFO
        if hasattr(self, 'mfassetclass'):
            assert self.tag == 'MFINFO'
            aggregate.mfassetclass = self.mfassetclass

        if hasattr(self, 'fimfassetclass'):
            assert self.tag == 'MFINFO'
            aggregate.fimfassetclass = self.fimfassetclass

        return aggregate


    def _flatten(self):
        """
        Recurse through aggregate and flatten; return an un-nested dict.

        This method will blow up if the aggregate contains LISTs, or if it
        contains multiple subaggregates whose namespaces will collide when
        flattened (e.g. BALAMT/DTASOF elements in LEDGERBAL and AVAILBAL).
        Remove all such hair from any element before passing it in here.
        """
        aggs = {}
        leaves = {}
        for child in self:
            tag = child.tag
            data = child.text or ''
            data = data.strip()
            if data:
                # it's a data-bearing leaf element.
                assert tag not in leaves
                # Silently drop all private tags (e.g. <INTU.XXXX>
                if '.' not in tag:
                    leaves[tag.lower()] = data
            else:
                # it's an aggregate.
                assert tag not in aggs
                aggs.update(child._flatten())
        # Double-check no key collisions as we flatten aggregates & leaves
        for key in aggs.keys():
            assert key not in leaves
        leaves.update(aggs)
        return leaves
