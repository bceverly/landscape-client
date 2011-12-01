from landscape.lib.hashlib import sha1

import apt_pkg


PACKAGE   = 1 << 0
PROVIDES  = 1 << 1
REQUIRES  = 1 << 2
UPGRADES  = 1 << 3
CONFLICTS = 1 << 4

DEB_PACKAGE       = 1 << 16 | PACKAGE
DEB_PROVIDES      = 2 << 16 | PROVIDES
DEB_NAME_PROVIDES = 3 << 16 | PROVIDES
DEB_REQUIRES      = 4 << 16 | REQUIRES
DEB_OR_REQUIRES   = 5 << 16 | REQUIRES
DEB_UPGRADES      = 6 << 16 | UPGRADES
DEB_CONFLICTS     = 7 << 16 | CONFLICTS


class PackageTypeError(Exception):
    """Raised when an unsupported package type is passed to build_skeleton."""


class PackageSkeleton(object):

    section = None
    summary = None
    description = None
    size = None
    installed_size = None

    def __init__(self, type, name, version):
        self.type = type
        self.name = name
        self.version = version
        self.relations = []

    def add_relation(self, type, info):
        self.relations.append((type, info))

    def get_hash(self):
        digest = sha1("[%d %s %s]" % (self.type, self.name, self.version))
        self.relations.sort()
        for pair in self.relations:
            digest.update("[%d %s]" % pair)
        return digest.digest()


def build_skeleton(pkg, with_info=False, with_unicode=False):
    if not build_skeleton.inited:
        build_skeleton.inited = True
        global DebPackage, DebNameProvides, DebOrDepends

        # Importing from backends depends on smart.init().
        from smart.backends.deb.base import (
            DebPackage, DebNameProvides, DebOrDepends)

    if not isinstance(pkg, DebPackage):
        raise PackageTypeError()

    if with_unicode:
        skeleton = PackageSkeleton(DEB_PACKAGE, unicode(pkg.name),
                                   unicode(pkg.version))
    else:
        skeleton = PackageSkeleton(DEB_PACKAGE, pkg.name, pkg.version)
    relations = set()
    for relation in pkg.provides:
        if isinstance(relation, DebNameProvides):
            relations.add((DEB_NAME_PROVIDES, str(relation)))
        else:
            relations.add((DEB_PROVIDES, str(relation)))
    for relation in pkg.requires:
        if isinstance(relation, DebOrDepends):
            relations.add((DEB_OR_REQUIRES, str(relation)))
        else:
            relations.add((DEB_REQUIRES, str(relation)))
    for relation in pkg.upgrades:
        relations.add((DEB_UPGRADES, str(relation)))
    for relation in pkg.conflicts:
        relations.add((DEB_CONFLICTS, str(relation)))

    skeleton.relations = sorted(relations)

    if with_info:
        info = pkg.loaders.keys()[0].getInfo(pkg)
        skeleton.section = info.getGroup()
        skeleton.summary = info.getSummary()
        skeleton.description = info.getDescription()
        skeleton.size = sum(info.getSize(url) for url in info.getURLs())
        skeleton.installed_size = info.getInstalledSize()

    return skeleton

build_skeleton.inited = False


def relation_to_string(relation_tuple):
    """Convert an apt relation to a string representation.

    @param relation_tuple: A tuple, (name, version, relation). version
        and relation can be the empty string, if the relation is on a
        name only.

    Returns something like "name > 1.0"
    """
    name, version, relation_type = relation_tuple
    relation_string = name
    if relation_type:
        relation_string += " %s %s" % (relation_type, version)
    return relation_string


def parse_record_field(record, record_field, relation_type,
                       or_relation_type=None):
    """Parse an apt C{Record} field and return skeleton relations

    @param record: An C{apt.package.Record} instance with package information.
    @param record_field: The name of the record field to parse.
    @param relation_type: The deb relation that can be passed to
        C{skeleton.add_relation()}
    @param or_relation_type: The deb relation that should be used if
        there is more than one value in a relation.
    """
    relations = set()
    values = apt_pkg.parse_depends(record.get(record_field, ""))
    for value in values:
        value_strings = [relation_to_string(relation) for relation in value]
        value_relation_type = relation_type
        if len(value_strings) > 1:
            value_relation_type = or_relation_type
        relation_string = " | ".join(value_strings)
        relations.add((value_relation_type, relation_string))
    return relations


def build_skeleton_apt(version, with_info=False, with_unicode=False):
    """Build a package skeleton from an apt package.

    @param version: An instance of C{apt.package.Version}
    @param with_info: Whether to extract extra information about the
        package, like description, summary, size.
    @param with_unicode: Whether the C{name} and C{version} of the
        skeleton should be unicode strings.
    """

    try:
        name, version_string = version.package.name, version.version
    except AttributeError:
        name, version_string = version.ParentPkg.Name, version.VerStr
    if with_unicode:
        name, version_string = unicode(name), unicode(version_string)
    skeleton = PackageSkeleton(DEB_PACKAGE, name, version_string)
    relations = set()
    #XXX: This is temporary, we should extract the record by other means.
    if hasattr(version, "record"):
        relations.update(parse_record_field(
            version.record, "Provides", DEB_PROVIDES))
    relations.add((DEB_NAME_PROVIDES, "%s = %s" % (name, version)))
    #XXX: This is temporary, we should extract the record by other means.
    if hasattr(version, "record"):
        relations.update(parse_record_field(
            version.record, "Pre-Depends", DEB_REQUIRES, DEB_OR_REQUIRES))
        relations.update(parse_record_field(
            version.record, "Depends", DEB_REQUIRES, DEB_OR_REQUIRES))

    relations.add((
        DEB_UPGRADES, "%s < %s" % (name, version)))

    #XXX: This is temporary, we should extract the record by other means.
    if hasattr(version, "record"):
        relations.update(parse_record_field(
            version.record, "Conflicts", DEB_CONFLICTS))
        relations.update(parse_record_field(
            version.record, "Breaks", DEB_CONFLICTS))
    skeleton.relations = sorted(relations)

    if with_info:
        skeleton.section = version.section
        skeleton.summary = version.summary
        skeleton.description = version.description
        skeleton.size = version.size
        if version.installed_size > 0:
            skeleton.installed_size = version.installed_size
        if with_unicode:
            skeleton.section = skeleton.section.decode("utf-8")
            skeleton.summary = skeleton.summary.decode("utf-8")
            skeleton.description = skeleton.description.decode("utf-8")
    return skeleton
