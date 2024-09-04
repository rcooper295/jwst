""" blendmeta - Merge metadata from multiple models to create
                   a new metadata instance and table

"""
from collections import OrderedDict

import numpy as np
from stdatamodels import schema as dm_schema

from . import blender


__all__ = [
    'interpret_attr_line',
    'interpret_entry',
    'KeywordRules',
    'KwRule',
]


# Custom blending functions
def multi(vals):
    """
    This will either return the common value from a list of identical values
    or 'MULTIPLE'
    """
    uniq_vals = list(set(vals))
    num_vals = len(uniq_vals)
    if num_vals == 0:
        return None
    if num_vals == 1:
        return uniq_vals[0]
    if num_vals > 1:
        return "MULTIPLE"


def first(items):
    """ Return first item from list of values"""
    if len(items):
        return items[0]
    return None


def last(items):
    """ Return last item from list of values"""
    if len(items):
        return items[-1]
    return None


# translation dictionary for function entries from rules files
blender_funcs = {'first': first,
                 'last': last,
                 'multi': multi,
                 'mean': np.mean,
                 'sum': np.sum,
                 'max': np.max,
                 'min': np.min,
                 # retained date/time names for backwards compatibility
                 # as these all assume ISO8601 format the lexical and
                 # chronological sorting match
                 'mintime': min,
                 'maxtime': max,
                 'mindate': min,
                 'maxdate': max,
                 'mindatetime': min,
                 'maxdatetime': max}


# Classes for managing keyword rules
class KeywordRules():

    def __init__(self, model):
        """ Read in the rules used to interpret the keywords from the specified
            instrument image header.
        """
        self.instrument = model.meta.instrument.name.lower()

        self.rule_specs = _build_schema_rules_dict(model._schema)

        self.rule_objects = []
        self.rules = []
        self.section_names = []

    def interpret_rules(self, hdrs):
        """ Convert specifications for rules from rules file
        into specific rules for this header(instrument/detector).

        Notes
        -----
        This allows for expansion rules to be applied to rules
        from the rules files (such as any wildcards or section titles).

        Output will be 'self.rules' that contains a list of tuples:
        - a tuple of 2 values for each column in the table
        - a tuple of 4 values for each attribute identified in metadata
        Partial sample from HST to show format:
        [('CTYPE1O', 'CTYPE1O'),
        ('CTYPE2O', 'CTYPE2O'),
        ('CUNIT1O', 'CUNIT1O'),
        ('CUNIT2O', 'CUNIT2O'),
        ('APERTURE', 'APERTURE', <function fitsblender.blendheaders.multi>, 'ignore'),
        ('DETECTOR', 'DETECTOR', <function fitsblender.blender.first>, 'ignore'),
        ('EXPEND', 'EXPEND', <function numpy.core.fromnumeric.amax>, 'ignore'),
        ('EXPSTART', 'EXPSTART', <function numpy.core.fromnumeric.amin>, 'ignore'),
        ('EXPTIME', 'TEXPTIME', <function numpy.core.fromnumeric.sum>, 'ignore'),
        ('EXPTIME', 'EXPTIME', <function numpy.core.fromnumeric.sum>, 'ignore')]

        This rules format will allow the algorithm, logic and code from
        the original fitsblender to be used with as little change as
        possible.  It will need to be derived (as with HST) from the
        input models metadata for expansion of attribute sections or
        wildcards in attributes specified in the rules.

        """
        if isinstance(hdrs, tuple):
            hdrs = list(hdrs)
        if not isinstance(hdrs, list):
            hdrs = [hdrs]

        # apply rules to headers
        for attr in self.rule_specs:
            speclist = self.rule_specs[attr]

            for rule in speclist:
                # Create KwRule input equivalent to HST rules input
                kwr = KwRule(rule)
                duplicate_rule = False

                for robj in self.rule_objects:
                    if kwr.rule_spec == robj.rule_spec:
                        duplicate_rule = True
                        break
                if not duplicate_rule:
                    for hdr in hdrs:
                        kwr.interpret(hdr)

                    self.rules.extend(kwr.rules)

    def merge(self, kwrules):
        """
        Merge a new set of interpreted rules into the current set
        The new rules, kwrules, can either be a new class or a whole new
        set of rules (like those obtained from using self.interpret_rules with
        a new header).
        """
        if isinstance(kwrules, KeywordRules):
            kwrules = kwrules.rules

        # Determine what rules are specified in kwrules that
        #    are NOT in self.rules
        k = []
        # Delete these extraneous rules from input kwrules
        for r in kwrules:
            if r not in self.rules:
                k.append(r)

        # extend self.rules with additional rules
        self.rules.extend(k)

    def apply(self, models):
        """ For a full list of metadata objects, apply the specified rules to
            generate a dictionary of new values and a table using
            blender.

            This method returns the new metadata object and summary table
            as `datamodels.model.ndmodel` and fits.binTableHDU objects.
        """
        # Apply rules to headers
        fbdict, fbtab = blender.metablender(models, self.rules)

        # Determine which keywords are included in the table but not
        # the new dict(header). These will be removed from the output
        # header altogether
        tabcols = fbtab.dtype.names

        # Start with a copy of the template as the new header
        # This will define what keywords need to be updated, as the rules
        # and input headers often include headers for multiple extensions in
        # order to build the complete table for all the keywords in the file
        # in one run
        new_model = models[0].copy()

        # Apply updated/blended values into new header, but only those
        # keywords which are already present in the 'template' new header
        # this allows the rules to be used on all extensions at once yet
        # update each extension separately without making copies of kws from
        # one extension to another.
        # new_model.update(fbdict)
        for attr in new_model.to_flat_dict():
            if 'meta' in attr and attr in fbdict:
                new_model[attr] = fbdict[attr]

        # Create summary table
        if len(tabcols) > 0:
            new_table = fbtab
        else:
            new_table = None
        return new_model, new_table


class KwRule():
    """
    This class encapsulates the logic needed for interpreting a single keyword
    rule from a text file.

    Notes
    -----
    The ``.rules`` attribute contains the interpreted set of rules that corresponds
    to this line.

    Example::

      Interpreting rule from
      {'meta.attribute': { 'rule': 'first'}}
      --or--
      {'meta.attribute': 'meta.attribute'}  # Table column specification

      into rule [('meta.attribute', 'meta.attribute', <function first at 0x7fe505db7668>, 'ignore')]
      and sname None

    """

    def __init__(self, line):
        """Initialize new keyword rule.

        Parameters
        ==========
        line : dict
            Line should be dict with attribute name as the key, and
            a dict as the value specifying 'rule'.
        """
        self.rule_spec = line  # dict read in from rules file
        self.rules = []
        self.delete_kws = []
        self.section_name = []

    def interpret(self, hdr):
        """Use metadata to interpret rule."""
        if self.rules:
            # If self.rules has already been defined for this rule, do not try
            # to interpret it any further with additional headers
            return
        irules, sname = interpret_entry(self.rule_spec, hdr)

        # keep track of any section name identified for this rule
        if sname:
            self.section_name.append(sname)

        # Now, interpret rule based on presence of kw in hdr
        if irules:
            self.rules = irules


# Utility functions.
def _build_schema_rules_dict(schema):
    """ Create a dict that extracts blend rules from an input schema.

    Parameters
    ----------
    schema : JSON schema fragment
        The schema in which to search.

    Returns
    -------
    results : OrderedDict
        Dictionary with schema attributes as keys and blend rules
        as values

    """
    def build_rules_dict(subschema, path, combiner, ctx, recurse):
        # Only interpret elements of the meta component of the model
        if len(path) > 1 and path[0] == 'meta' and 'items' not in path:
            for combiner in ['anyOf', 'oneOf']:
                if combiner in path:
                    path = path[:path.index(combiner)]
                    break
            attr = '.'.join(path)
            if subschema.get('properties'):
                return  # Ignore ObjectNodes

            # Get blending info
            kwrule = subschema.get('blend_rule')
            kwtab = subschema.get('blend_table')
            kwname = subschema.get('fits_keyword', attr)

            # If rules had already been set, only modify
            # the rules if there are explicit settings.
            rule_spec = None
            result = results.get(attr, [])
            if kwrule:
                rule_spec = {attr: {'rule': kwrule}}
            elif not results.get(attr):
                rule_spec = {attr: {'rule': 'first'}}
            if rule_spec:
                result.append(rule_spec)

            # Add keyword to table if specified.
            if kwtab:
                result.append({attr: kwname})

            # Add the results back.
            if len(result):
                results[attr] = result

        else:
            return

    results = OrderedDict()
    dm_schema.walk_schema(schema, build_rules_dict, results)
    return results


def interpret_entry(line, hdr):
    """ Generate the rule(s) specified by the entry from the rules file.

    Notes
    -----
    The entry should always be a dict with format:
    {attribute_name : {'rule':'some_rule'}}
    -- or (for table column specification)--
    {attribute_name: attribute_name}
    """
    # Interpret raw input line
    attr = list(line.keys())[0]
    line_spec = line[attr]
    attr_column = True  # Determine whether this rule defines a table column
    if isinstance(line_spec, dict):
        attr_column = False  # If not, turn this off

    # Initialize output values
    rules = []
    section_name = None

    # Parse the line
    attr_rules = interpret_attr_line(attr, line_spec)
    rules.extend(attr_rules)

    return rules, section_name


def interpret_attr_line(attr, line_spec):
    """ Generate rule for single attribute from input line from rules file."""
    rules = []

    kws = [attr]
    if isinstance(line_spec, dict):
        kws2 = kws
    else:
        kws2 = [line_spec]

    lrule = None
    if 'rule' in line_spec:
        lrule = line_spec['rule']

    # Interpret short-hand rules using dict
    if lrule is not None and len(lrule) > 0:
        if lrule in blender_funcs:
            lrule = blender_funcs[lrule]
        else:
            lrule = None
        # build separate rule for each kw
        for kw1, kw2 in zip(kws, kws2):
            new_rule = (kw1, kw2, lrule)
            if new_rule not in rules:
                rules.append(new_rule)
    else:
        for kw1, kw2 in zip(kws, kws2):
            new_rule = (kw1, kw2)
            if new_rule not in rules:
                rules.append(new_rule)

    return rules
