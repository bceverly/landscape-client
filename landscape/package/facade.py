import hashlib
import os
import subprocess
import tempfile
from cStringIO import StringIO
from operator import attrgetter

has_smart = True
try:
    import smart
    from smart.transaction import (
        Transaction, PolicyInstall, PolicyUpgrade, PolicyRemove, Failed)
    from smart.const import INSTALL, REMOVE, UPGRADE, ALWAYS, NEVER
except ImportError:
    has_smart = False

# Importing apt throws a FutureWarning on hardy, that we don't want to
# see.
import warnings
warnings.filterwarnings("ignore", module="apt", category=FutureWarning)
del warnings

import apt
import apt_inst
import apt_pkg

has_new_enough_apt = True
from aptsources.sourceslist import SourcesList
try:
    from apt.progress.text import AcquireProgress
    from apt.progress.base import InstallProgress
except ImportError:
    AcquireProgress = object
    InstallProgress = object
    has_new_enough_apt = False

from landscape.lib.fs import append_file, create_file, read_file
from landscape.constants import UBUNTU_PATH
from landscape.package.skeleton import build_skeleton, build_skeleton_apt


class TransactionError(Exception):
    """Raised when the transaction fails to run."""


class DependencyError(Exception):
    """Raised when a needed dependency wasn't explicitly marked."""

    def __init__(self, packages):
        self.packages = packages

    def __str__(self):
        return ("Missing dependencies: %s" %
                ", ".join([str(package) for package in self.packages]))


class SmartError(Exception):
    """Raised when Smart fails in an undefined way."""


class ChannelError(Exception):
    """Raised when channels fail to load."""


class LandscapeAcquireProgress(AcquireProgress):

    def _winch(self, *dummy):
        """Override trying to get the column count of the buffer.

        We always send the output to a file, not to a terminal, so the
        default width (80 columns) is fine for us.

        Overriding this method means that we don't have to care about
        fcntl.ioctl API differences for different Python versions.
        """


class LandscapeInstallProgress(InstallProgress):

    dpkg_exited = None

    def wait_child(self):
        """Override to find out whether dpkg exited or not.

        The C{run()} method returns os.WEXITSTATUS(res) without checking
        os.WIFEXITED(res) first, so it can signal that everything is ok,
        even though something causes dpkg not to exit cleanly.

        Save whether dpkg exited cleanly into the C{dpkg_exited}
        attribute. If dpkg exited cleanly the exit code can be used to
        determine whether there were any errors. If dpkg didn't exit
        cleanly it should mean that something went wrong.
        """
        res = super(LandscapeInstallProgress, self).wait_child()
        self.dpkg_exited = os.WIFEXITED(res)
        return res


class AptFacade(object):
    """Wrapper for tasks using Apt.

    This object wraps Apt features, in a way that makes using and testing
    these features slightly more comfortable.

    @param root: The root dir of the Apt configuration files.
    @ivar refetch_package_index: Whether to refetch the package indexes
        when reloading the channels, or reuse the existing local
        database.
    """

    supports_package_holds = True
    supports_package_locks = False
    _dpkg_status = "/var/lib/dpkg/status"

    def __init__(self, root=None):
        self._root = root
        self._dpkg_args = []
        if self._root is not None:
            self._ensure_dir_structure()
            self._dpkg_args.extend(["--root", self._root])
        # don't use memonly=True here because of a python-apt bug on Natty when
        # sources.list contains invalid lines (LP: #886208)
        self._cache = apt.cache.Cache(rootdir=root)
        self._channels_loaded = False
        self._pkg2hash = {}
        self._hash2pkg = {}
        self._version_installs = []
        self._global_upgrade = False
        self._version_removals = []
        self._version_hold_creations = []
        self._version_hold_removals = []
        self.refetch_package_index = False

    def _ensure_dir_structure(self):
        self._ensure_sub_dir("etc/apt")
        self._ensure_sub_dir("etc/apt/sources.list.d")
        self._ensure_sub_dir("var/cache/apt/archives/partial")
        self._ensure_sub_dir("var/lib/apt/lists/partial")
        dpkg_dir = self._ensure_sub_dir("var/lib/dpkg")
        self._ensure_sub_dir("var/lib/dpkg/info")
        self._ensure_sub_dir("var/lib/dpkg/updates")
        self._ensure_sub_dir("var/lib/dpkg/triggers")
        create_file(os.path.join(dpkg_dir, "available"), "")
        self._dpkg_status = os.path.join(dpkg_dir, "status")
        if not os.path.exists(self._dpkg_status):
            create_file(self._dpkg_status, "")

    def _ensure_sub_dir(self, sub_dir):
        """Ensure that a dir in the Apt root exists."""
        full_path = os.path.join(self._root, sub_dir)
        if not os.path.exists(full_path):
            os.makedirs(full_path)
        return full_path

    def deinit(self):
        """This method exists solely to be compatible with C{SmartFacade}."""

    def get_packages(self):
        """Get all the packages available in the channels."""
        return self._hash2pkg.itervalues()

    def get_locked_packages(self):
        """Get all packages in the channels that are locked.

        For Apt, it means all packages that are held.
        """
        return [
            version for version in self.get_packages()
            if (self.is_package_installed(version)
                and self._is_package_held(version.package))]

    def get_package_locks(self):
        """Return all set package locks.

        @return: A C{list} of ternary tuples, contaning the name, relation
            and version details for each lock currently set on the system.

        XXX: This method isn't implemented yet. It's here to make the
        transition to Apt in the package reporter easier.
        """
        return []

    def get_package_holds(self):
        """Return the name of all the packages that are on hold."""
        return [version.package.name for version in self.get_locked_packages()]

    def _set_dpkg_selections(self, selection):
        """Set the dpkg selection.

        It basically does "echo $selection | dpkg --set-selections".
        """
        process = subprocess.Popen(
            ["dpkg", "--set-selections"] + self._dpkg_args,
            stdin=subprocess.PIPE)
        process.communicate(selection)

    def set_package_hold(self, version):
        """Add a dpkg hold for a package.

        @param version: The version of the package to hold.
        """
        self._set_dpkg_selections(version.package.name + " hold")

    def remove_package_hold(self, version):
        """Removes a dpkg hold for a package.

        @param version: The version of the package to unhold.
        """
        if (not self.is_package_installed(version) or
            not self._is_package_held(version.package)):
            return
        self._set_dpkg_selections(version.package.name + " install")

    def reload_channels(self, force_reload_binaries=False):
        """Reload the channels and update the cache.

        @param force_reload_binaries: Whether to always reload
            information about the binaries packages that are in the facade's
            internal repo.
        """
        self._cache.open(None)
        internal_sources_list = self._get_internal_sources_list()
        if (self.refetch_package_index or
            (force_reload_binaries and os.path.exists(internal_sources_list))):
            # Try to update only the internal repos, if the python-apt
            # version is new enough to accept a sources_list parameter.
            new_apt_args = {}
            if force_reload_binaries and not self.refetch_package_index:
                new_apt_args["sources_list"] = internal_sources_list
            try:
                try:
                    self._cache.update(**new_apt_args)
                except TypeError:
                    self._cache.update()
            except apt.cache.FetchFailedException:
                raise ChannelError(
                    "Apt failed to reload channels (%r)" % (
                        self.get_channels()))
            self._cache.open(None)

        self._pkg2hash.clear()
        self._hash2pkg.clear()
        for package in self._cache:
            if not self._is_main_architecture(package):
                continue
            for version in package.versions:
                hash = self.get_package_skeleton(
                    version, with_info=False).get_hash()
                # Use a tuple including the package, since the Version
                # objects of two different packages can have the same
                # hash.
                self._pkg2hash[(package, version)] = hash
                self._hash2pkg[hash] = version
        self._channels_loaded = True

    def ensure_channels_reloaded(self):
        """Reload the channels if they haven't been reloaded yet."""
        if self._channels_loaded:
            return
        self.reload_channels()

    def _get_internal_sources_list(self):
        """Return the path to the source.list file for the facade channels."""
        sources_dir = apt_pkg.config.find_dir("Dir::Etc::sourceparts")
        return os.path.join(sources_dir, "_landscape-internal-facade.list")

    def add_channel_apt_deb(self, url, codename, components=None):
        """Add a deb URL which points to a repository.

        @param url: The base URL of the repository.
        @param codename: The dist in the repository.
        @param components: The components to be included.
        """
        sources_file_path = self._get_internal_sources_list()
        sources_line = "deb %s %s" % (url, codename)
        if components:
            sources_line += " %s" % " ".join(components)
        if os.path.exists(sources_file_path):
            current_content = read_file(sources_file_path).split("\n")
            if sources_line in current_content:
                return
        sources_line += "\n"
        append_file(sources_file_path, sources_line)

    def add_channel_deb_dir(self, path):
        """Add a directory with packages as a channel.

        @param path: The path to the directory containing the packages.

        A Packages file is created in the directory with information
        about the deb files.
        """
        self._create_packages_file(path)
        self.add_channel_apt_deb("file://%s" % path, "./", None)

    def clear_channels(self):
        """Clear the channels that have been added through the facade.

        Channels that weren't added through the facade (i.e.
        /etc/apt/sources.list and /etc/apt/sources.list.d) won't be
        removed.
        """
        sources_file_path = self._get_internal_sources_list()
        if os.path.exists(sources_file_path):
            os.remove(sources_file_path)

    def _create_packages_file(self, deb_dir):
        """Create a Packages file in a directory with debs."""
        packages_contents = "\n".join(
            self.get_package_stanza(os.path.join(deb_dir, filename))
            for filename in sorted(os.listdir(deb_dir)))
        create_file(os.path.join(deb_dir, "Packages"), packages_contents)

    def get_channels(self):
        """Return a list of channels configured.

        A channel is a deb line in sources.list or sources.list.d. It's
        represented by a dict with baseurl, distribution, components,
        and type keys.
        """
        sources_list = SourcesList()
        return [{"baseurl": entry.uri, "distribution": entry.dist,
                 "components": " ".join(entry.comps), "type": entry.type}
                for entry in sources_list if not entry.disabled]

    def reset_channels(self):
        """Remove all the configured channels."""
        sources_list = SourcesList()
        for entry in sources_list:
            entry.set_enabled(False)
        sources_list.save()

    def get_package_stanza(self, deb_path):
        """Return a stanza for the package to be included in a Packages file.

        @param deb_path: The path to the deb package.
        """
        deb_file = open(deb_path)
        deb = apt_inst.DebFile(deb_file)
        control = deb.control.extractdata("control")
        deb_file.close()
        filename = os.path.basename(deb_path)
        size = os.path.getsize(deb_path)
        contents = read_file(deb_path)
        md5 = hashlib.md5(contents).hexdigest()
        sha1 = hashlib.sha1(contents).hexdigest()
        sha256 = hashlib.sha256(contents).hexdigest()
        # Use rewrite_section to ensure that the field order is correct.
        return apt_pkg.rewrite_section(
            apt_pkg.TagSection(control), apt_pkg.REWRITE_PACKAGE_ORDER,
            [("Filename", filename), ("Size", str(size)),
             ("MD5sum", md5), ("SHA1", sha1), ("SHA256", sha256)])

    def get_arch(self):
        """Return the architecture APT is configured to use."""
        return apt_pkg.config.get("APT::Architecture")

    def set_arch(self, architecture):
        """Set the architecture that APT should use.

        Setting multiple architectures isn't supported.
        """
        if architecture is None:
            architecture = ""
        # From oneiric and onwards Architectures is used to set which
        # architectures can be installed, in case multiple architectures
        # are supported. We force it to be single architecture, until we
        # have a plan for supporting multiple architectures.
        apt_pkg.config.clear("APT::Architectures")
        apt_pkg.config.set("APT::Architectures::", architecture)
        result = apt_pkg.config.set("APT::Architecture", architecture)
        # Reload the cache, otherwise architecture change isn't reflected in
        # package list
        self._cache.open(None)
        return result

    def get_package_skeleton(self, pkg, with_info=True):
        """Return a skeleton for the provided package.

        The skeleton represents the basic structure of the package.

        @param pkg: Package to build skeleton from.
        @param with_info: If True, the skeleton will include information
            useful for sending data to the server.  Such information isn't
            necessary if the skeleton will be used to build a hash.

        @return: a L{PackageSkeleton} object.
        """
        return build_skeleton_apt(pkg, with_info=with_info, with_unicode=True)

    def get_package_hash(self, version):
        """Return a hash from the given package.

        @param version: an L{apt.package.Version} object.
        """
        return self._pkg2hash.get((version.package, version))

    def get_package_hashes(self):
        """Get the hashes of all the packages available in the channels."""
        return self._pkg2hash.values()

    def get_package_by_hash(self, hash):
        """Get the package having the provided hash.

        @param hash: The hash the package should have.

        @return: The L{apt.package.Package} that has the given hash.
        """
        return self._hash2pkg.get(hash)

    def is_package_installed(self, version):
        """Is the package version installed?"""
        return version == version.package.installed

    def is_package_available(self, version):
        """Is the package available for installation?"""
        return version.downloadable

    def is_package_upgrade(self, version):
        """Is the package an upgrade for another installed package?"""
        if not version.package.is_upgradable or not version.package.installed:
            return False
        return version > version.package.installed

    def _is_main_architecture(self, package):
        """Is the package for the facade's main architecture?"""
        # package.name includes the architecture, if it's for a foreign
        # architectures. package.shortname never includes the
        # architecture. package.shortname doesn't exist on releases that
        # don't support multi-arch, though.
        if not hasattr(package, "shortname"):
            return True
        return package.name == package.shortname

    def _is_package_held(self, package):
        """Is the package marked as held?"""
        return package._pkg.selected_state == apt_pkg.SELSTATE_HOLD

    def get_packages_by_name(self, name):
        """Get all available packages matching the provided name.

        @param name: The name the returned packages should have.
        """
        return [
            version for version in self.get_packages()
            if version.package.name == name]

    def _get_broken_packages(self):
        """Return the packages that are in a broken state."""
        return set(
            version.package for version in self.get_packages()
            if version.package.is_inst_broken)

    def _get_changed_versions(self, package):
        """Return the versions that will be changed for the package.

        Apt gives us that a package is going to be changed and have
        variables set on the package to indicate what will change. We
        need to convert that into a list of versions that will be either
        installed or removed, which is what the server expects to get.
        """
        if package.marked_install:
            return [package.candidate]
        if package.marked_upgrade or package.marked_downgrade:
            return [package.installed, package.candidate]
        if package.marked_delete:
            return [package.installed]
        return None

    def _check_changes(self, requested_changes):
        """Check that the changes Apt will do have all been requested.

        @raises DependencyError: If some change hasn't been explicitly
            requested.
        @return: C{True} if all the changes that Apt will perform have
            been requested.
        """
        # Build tuples of (package, version) so that we can do
        # comparison checks. Same versions of different packages compare
        # as being the same, so we need to include the package as well.
        all_changes = [
            (version.package, version) for version in requested_changes]
        versions_to_be_changed = set()
        for package in self._cache.get_changes():
            if not self._is_main_architecture(package):
                continue
            versions = self._get_changed_versions(package)
            versions_to_be_changed.update(
                (package, version) for version in versions)
        dependencies = versions_to_be_changed.difference(all_changes)
        if dependencies:
            raise DependencyError(
                [version for package, version in dependencies])
        return len(versions_to_be_changed) > 0

    def _get_unmet_relation_info(self, dep_relation):
        """Return a string representation of a specific dependency relation."""
        info = dep_relation.target_pkg.name
        if dep_relation.target_ver:
            info += " (%s %s)" % (
                dep_relation.comp_type, dep_relation.target_ver)
        reason = " but is not installable"
        if dep_relation.target_pkg.name in self._cache:
            dep_package = self._cache[dep_relation.target_pkg.name]
            if dep_package.installed or dep_package.marked_install:
                version = dep_package.candidate.version
                if dep_package not in self._cache.get_changes():
                    version = dep_package.installed.version
                reason = " but %s is to be installed" % version
        info += reason
        return info

    def _is_dependency_satisfied(self, dependency, dep_type):
        """Return whether a dependency is satisfied.

        For positive dependencies (Pre-Depends, Depends) it means that
        one of its targets is going to be installed. For negative
        dependencies (Conflicts, Breaks), it means that none of its
        targets are going to be installed.
        """
        is_positive = dep_type not in ["Breaks", "Conflicts"]
        depcache = self._cache._depcache
        for or_dep in dependency:
            for target in or_dep.all_targets():
                package = target.parent_pkg
                if ((package.current_state == apt_pkg.CURSTATE_INSTALLED
                     or depcache.marked_install(package))
                    and not depcache.marked_delete(package)):
                    return is_positive
        return not is_positive

    def _get_unmet_dependency_info(self):
        """Get information about unmet dependencies in the cache state.

        Go through all the broken packages and say which dependencies
        haven't been satisfied.

        @return: A string with dependency information like what you get
            from apt-get.
        """

        broken_packages = self._get_broken_packages()
        if not broken_packages:
            return ""
        all_info = ["The following packages have unmet dependencies:"]
        for package in sorted(broken_packages, key=attrgetter("name")):
            for dep_type in ["PreDepends", "Depends", "Conflicts", "Breaks"]:
                dependencies = package.candidate._cand.depends_list.get(
                    dep_type, [])
                for dependency in dependencies:
                    if self._is_dependency_satisfied(dependency, dep_type):
                        continue
                    relation_infos = []
                    for dep_relation in dependency:
                        relation_infos.append(
                            self._get_unmet_relation_info(dep_relation))
                    info = "  %s: %s: " % (package.name, dep_type)
                    or_divider = " or\n" + " " * len(info)
                    all_info.append(info + or_divider.join(relation_infos))
        return "\n".join(all_info)

    def perform_changes(self):
        """Perform the pending package operations."""
        # Try to enforce non-interactivity
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"
        os.environ["APT_LISTCHANGES_FRONTEND"] = "none"
        os.environ["APT_LISTBUGS_FRONTEND"] = "none"
        # dpkg will fail if no path is set.
        if "PATH" not in os.environ:
            os.environ["PATH"] = UBUNTU_PATH
        apt_pkg.config.clear("DPkg::options")
        apt_pkg.config.set("DPkg::options::", "--force-confold")

        held_package_names = set()
        package_installs = set(
            version.package for version in self._version_installs)
        package_upgrades = set(
            version.package for version in self._version_removals
            if version.package in package_installs)
        version_changes = self._version_installs[:]
        version_changes.extend(self._version_removals)
        hold_changes = self._version_hold_creations[:]
        hold_changes.extend(self._version_hold_removals)
        if (not hold_changes and not version_changes and
            not self._global_upgrade):
            return None
        if hold_changes:
            not_installed = filter(
                lambda version: not self.is_package_installed(version),
                self._version_hold_creations)
            if not_installed:
                raise TransactionError(
                    "Cannot perform the changes, since the following " +
                    "packages are not installed: %s" % ", ".join(
                        [version.package.name
                         for version in sorted(not_installed)]))

            else:
                for version in self._version_hold_creations:
                    self.set_package_hold(version)

            for version in self._version_hold_removals:
                self.remove_package_hold(version)

            result_text = "Package holds successfully changed."
        if version_changes or self._global_upgrade:
            fixer = apt_pkg.ProblemResolver(self._cache._depcache)
            already_broken_packages = self._get_broken_packages()
            for version in self._version_installs:
                # Set the candidate version, so that the version we want to
                # install actually is the one getting installed.
                version.package.candidate = version
                version.package.mark_install(auto_fix=False)
                # If we need to resolve dependencies, try avoiding having
                # the package we asked to be installed from being removed.
                # (This is what would have been done if auto_fix would have
                # been True.
                fixer.clear(version.package._pkg)
                fixer.protect(version.package._pkg)
            if self._global_upgrade:
                self._cache.upgrade(dist_upgrade=True)
            for version in self._version_removals:
                if self._is_package_held(version.package):
                    held_package_names.add(version.package.name)
                if version.package in package_upgrades:
                    # The server requests the old version to be removed for
                    # upgrades, since Smart worked that way. For Apt we have
                    # to take care not to mark upgraded packages for # removal.
                    continue
                version.package.mark_delete(auto_fix=False)
                # Configure the resolver in the same way
                # mark_delete(auto_fix=True) would have done.
                fixer.clear(version.package._pkg)
                fixer.protect(version.package._pkg)
                fixer.remove(version.package._pkg)
                fixer.install_protect()

            if held_package_names:
                raise TransactionError(
                    "Can't perform the changes, since the following packages" +
                    " are held: %s" % ", ".join(sorted(held_package_names)))

            now_broken_packages = self._get_broken_packages()
            if now_broken_packages != already_broken_packages:
                try:
                    fixer.resolve(True)
                except SystemError, error:
                    raise TransactionError(error.args[0] + "\n" +
                                           self._get_unmet_dependency_info())
            if not self._check_changes(version_changes):
                return None
            fetch_output = StringIO()
            # Redirect stdout and stderr to a file. We need to work with the
            # file descriptors, rather than sys.stdout/stderr, since dpkg is
            # run in a subprocess.
            fd, install_output_path = tempfile.mkstemp()
            old_stdout = os.dup(1)
            old_stderr = os.dup(2)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            install_progress = LandscapeInstallProgress()
            try:
                self._cache.commit(
                    fetch_progress=LandscapeAcquireProgress(fetch_output),
                    install_progress=install_progress)
                if not install_progress.dpkg_exited:
                    raise SystemError("dpkg didn't exit cleanly.")
            except SystemError, error:
                result_text = (
                    fetch_output.getvalue() + read_file(install_output_path))
                raise TransactionError(error.args[0] +
                                       "\n\nPackage operation log:\n" +
                                       result_text)
            else:
                result_text = (
                    fetch_output.getvalue() + read_file(install_output_path))
            finally:
                # Restore stdout and stderr.
                os.dup2(old_stdout, 1)
                os.dup2(old_stderr, 2)
                os.remove(install_output_path)
        return result_text

    def reset_marks(self):
        """Clear the pending package operations."""
        del self._version_installs[:]
        del self._version_removals[:]
        del self._version_hold_removals[:]
        del self._version_hold_creations[:]
        self._global_upgrade = False
        self._cache.clear()

    def mark_install(self, version):
        """Mark the package for installation."""
        self._version_installs.append(version)

    def mark_global_upgrade(self):
        """Upgrade all installed packages."""
        self._global_upgrade = True

    def mark_remove(self, version):
        """Mark the package for removal."""
        self._version_removals.append(version)

    def mark_create_hold(self, version):
        """Mark the package to be held."""
        self._version_hold_creations.append(version)

    def mark_remove_hold(self, version):
        """Mark the package to have its hold removed."""
        self._version_hold_removals.append(version)


class SmartFacade(object):
    """Wrapper for tasks using Smart.

    This object wraps Smart features, in a way that makes using and testing
    these features slightly more comfortable.

    @param smart_init_kwargs: A dictionary that can be used to pass specific
        keyword parameters to to L{smart.init}.
    """

    _deb_package_type = None
    supports_package_holds = False
    supports_package_locks = True

    def __init__(self, smart_init_kwargs={}, sysconf_args=None):
        if not has_smart:
            raise RuntimeError(
                "Smart needs to be installed if SmartFacade is used.")
        self._smart_init_kwargs = smart_init_kwargs.copy()
        self._smart_init_kwargs.setdefault("interface", "landscape")
        self._sysconfig_args = sysconf_args or {}
        self._reset()

    def _reset(self):
        # This attribute is initialized lazily in the _get_ctrl() method.
        self._ctrl = None
        self._pkg2hash = {}
        self._hash2pkg = {}
        self._marks = {}
        self._caching = ALWAYS
        self._channels_reloaded = False

    def deinit(self):
        """Deinitialize the Facade and the Smart library."""
        if self._ctrl:
            smart.deinit()
        self._reset()

    def _get_ctrl(self):
        if self._ctrl is None:
            if self._smart_init_kwargs.get("interface") == "landscape":
                from landscape.package.interface import (
                    install_landscape_interface)
                install_landscape_interface()
            self._ctrl = smart.init(**self._smart_init_kwargs)
            for key, value in self._sysconfig_args.items():
                smart.sysconf.set(key, value, soft=True)
            smart.initDistro(self._ctrl)
            smart.initPlugins()
            smart.sysconf.set("pm-iface-output", True, soft=True)
            smart.sysconf.set("deb-non-interactive", True, soft=True)

            # We can't import it before hand because reaching .deb.* depends
            # on initialization (yeah, sucky).
            from smart.backends.deb.base import DebPackage
            self._deb_package_type = DebPackage

            self.smart_initialized()
        return self._ctrl

    def smart_initialized(self):
        """Hook called when the Smart library is initialized."""

    def ensure_channels_reloaded(self):
        """Reload the channels if they haven't been reloaded yet."""
        if self._channels_reloaded:
            return
        self._channels_reloaded = True
        self.reload_channels()

    def reload_channels(self, force_reload_binaries=False):
        """
        Reload Smart channels, getting all the cache (packages) in memory.

        @raise: L{ChannelError} if Smart fails to reload the channels.
        """
        ctrl = self._get_ctrl()

        try:
            reload_result = ctrl.reloadChannels(caching=self._caching)
        except smart.Error:
            failed = True
        else:
            # Raise an error only if we are trying to download remote lists
            failed = not reload_result and self._caching == NEVER
        if failed:
            raise ChannelError("Smart failed to reload channels (%s)"
                               % smart.sysconf.get("channels"))

        self._hash2pkg.clear()
        self._pkg2hash.clear()

        for pkg in self.get_packages():
            hash = self.get_package_skeleton(pkg, False).get_hash()
            self._hash2pkg[hash] = pkg
            self._pkg2hash[pkg] = hash

        self.channels_reloaded()

    def channels_reloaded(self):
        """Hook called after Smart channels are reloaded."""

    def get_package_skeleton(self, pkg, with_info=True):
        """Return a skeleton for the provided package.

        The skeleton represents the basic structure of the package.

        @param pkg: Package to build skeleton from.
        @param with_info: If True, the skeleton will include information
            useful for sending data to the server.  Such information isn't
            necessary if the skeleton will be used to build a hash.

        @return: a L{PackageSkeleton} object.
        """
        return build_skeleton(pkg, with_info)

    def get_package_hash(self, pkg):
        """Return a hash from the given package.

        @param pkg: a L{smart.backends.deb.base.DebPackage} objects
        """
        return self._pkg2hash.get(pkg)

    def get_package_hashes(self):
        """Get the hashes of all the packages available in the channels."""
        return self._pkg2hash.values()

    def get_packages(self):
        """
        Get all the packages available in the channels.

        @return: a C{list} of L{smart.backends.deb.base.DebPackage} objects
        """
        return [pkg for pkg in self._get_ctrl().getCache().getPackages()
                if isinstance(pkg, self._deb_package_type)]

    def get_locked_packages(self):
        """Get all packages in the channels matching the set locks."""
        return smart.pkgconf.filterByFlag("lock", self.get_packages())

    def get_packages_by_name(self, name):
        """
        Get all available packages matching the provided name.

        @return: a C{list} of L{smart.backends.deb.base.DebPackage} objects
        """
        return [pkg for pkg in self._get_ctrl().getCache().getPackages(name)
                if isinstance(pkg, self._deb_package_type)]

    def get_package_by_hash(self, hash):
        """
        Get all available packages matching the provided hash.

        @return: a C{list} of L{smart.backends.deb.base.DebPackage} objects
        """
        return self._hash2pkg.get(hash)

    def mark_install(self, pkg):
        self._marks[pkg] = INSTALL

    def mark_remove(self, pkg):
        self._marks[pkg] = REMOVE

    def mark_upgrade(self, pkg):
        self._marks[pkg] = UPGRADE

    def mark_global_upgrade(self):
        """Upgrade all installed packages."""
        for package in self.get_packages():
            if self.is_package_installed(package):
                self.mark_upgrade(package)

    def mark_create_hold(self, version):
        """Mark the package to be held."""
        raise NotImplementedError

    def mark_remove_hold(self, version):
        """Mark the package to have its hold removed."""
        raise NotImplementedError

    def reset_marks(self):
        self._marks.clear()

    def perform_changes(self):
        ctrl = self._get_ctrl()
        cache = ctrl.getCache()

        transaction = Transaction(cache)

        policy = PolicyInstall

        only_remove = True
        for pkg, oper in self._marks.items():
            if oper == UPGRADE:
                policy = PolicyUpgrade
            if oper != REMOVE:
                only_remove = False
            transaction.enqueue(pkg, oper)

        if only_remove:
            policy = PolicyRemove

        transaction.setPolicy(policy)

        try:
            transaction.run()
        except Failed, e:
            raise TransactionError(e.args[0])
        changeset = transaction.getChangeSet()

        if not changeset:
            return None  # Nothing to do.

        missing = []
        for pkg, op in changeset.items():
            if self._marks.get(pkg) != op:
                missing.append(pkg)
        if missing:
            raise DependencyError(missing)

        try:
            self._ctrl.commitChangeSet(changeset)
        except smart.Error, e:
            raise TransactionError(e.args[0])

        output = smart.iface.get_output_for_landscape()
        failed = smart.iface.has_failed_for_landscape()

        smart.iface.reset_for_landscape()

        if failed:
            raise SmartError(output)
        return output

    def reload_cache(self):
        cache = self._get_ctrl().getCache()
        cache.reset()
        cache.load()

    def get_arch(self):
        """
        Get the host dpkg architecture.
        """
        self._get_ctrl()
        from smart.backends.deb.loader import DEBARCH
        return DEBARCH

    def set_arch(self, arch):
        """
        Set the host dpkg architecture.

        To take effect it must be called before L{reload_channels}.

        @param arch: the dpkg architecture to use (e.g. C{"i386"})
        """
        self._get_ctrl()
        smart.sysconf.set("deb-arch", arch)

        # XXX workaround Smart setting DEBARCH statically in the
        # smart.backends.deb.base module
        import smart.backends.deb.loader as loader
        loader.DEBARCH = arch

    def set_caching(self, mode):
        """
        Set Smart's caching mode.

        @param mode: The caching mode to pass to Smart's C{reloadChannels}
            when calling L{reload_channels} (e.g C{smart.const.NEVER} or
            C{smart.const.ALWAYS}).
        """
        self._caching = mode

    def reset_channels(self):
        """Remove all configured Smart channels."""
        self._get_ctrl()
        smart.sysconf.set("channels", {}, soft=True)

    def add_channel(self, alias, channel):
        """
        Add a Smart channel.

        This method can be called more than once to set multiple channels.
        To take effect it must be called before L{reload_channels}.

        @param alias: A string identifying the channel to be added.
        @param channel: A C{dict} holding information about the channel to
            add (see the Smart API for details about valid keys and values).
        """
        channels = self.get_channels()
        channels.update({alias: channel})
        smart.sysconf.set("channels", channels, soft=True)

    def add_channel_apt_deb(self, url, codename, components):
        """Add a Smart channel of type C{"apt-deb"}.

        @see: L{add_channel}
        """
        alias = codename
        channel = {"baseurl": url, "distribution": codename,
                   "components": components, "type": "apt-deb"}
        self.add_channel(alias, channel)

    def add_channel_deb_dir(self, path):
        """Add a Smart channel of type C{"deb-dir"}.

        @see: L{add_channel}
        """
        alias = path
        channel = {"path": path, "type": "deb-dir"}
        self.add_channel(alias, channel)

    def clear_channels(self):
        """Clear channels.

        This method exists to be compatible with AptFacade. Smart
        doesn't need to clear its channels.
        """

    def get_channels(self):
        """
        @return: A C{dict} of all configured channels.
        """
        self._get_ctrl()
        return smart.sysconf.get("channels")

    def get_package_locks(self):
        """Return all set package locks.

        @return: A C{list} of ternary tuples, contaning the name, relation
            and version details for each lock currently set on the system.
        """
        self._get_ctrl()
        locks = []
        locks_by_name = smart.pkgconf.getFlagTargets("lock")
        for name in locks_by_name:
            for condition in locks_by_name[name]:
                relation = condition[0] or ""
                version = condition[1] or ""
                locks.append((name, relation, version))
        return locks

    def _validate_lock_condition(self, relation, version):
        if relation and not version:
            raise RuntimeError("Package lock version not provided")
        if version and not relation:
            raise RuntimeError("Package lock relation not provided")

    def set_package_lock(self, name, relation=None, version=None):
        """Set a new package lock.

        Any package matching the given name and possibly the given version
        condition will be locked.

        @param name: The name a package must match in order to be locked.
        @param relation: Optionally, the relation of the version condition the
            package must satisfy in order to be considered as locked.
        @param version: Optionally, the version associated with C{relation}.

        @note: If used at all, the C{relation} and C{version} parameter must be
           both provided.
        """
        self._validate_lock_condition(relation, version)
        self._get_ctrl()
        smart.pkgconf.setFlag("lock", name, relation, version)

    def remove_package_lock(self, name, relation=None, version=None):
        """Remove a package lock."""
        self._validate_lock_condition(relation, version)
        self._get_ctrl()
        smart.pkgconf.clearFlag("lock", name=name, relation=relation,
                                version=version)

    def save_config(self):
        """Flush the current smart configuration to disk."""
        control = self._get_ctrl()
        control.saveSysConf()

    def is_package_installed(self, package):
        """Is the package installed?"""
        return package.installed

    def is_package_available(self, package):
        """Is the package available for installation?"""
        for loader in package.loaders:
            # Is the package also in a non-installed
            # loader?  IOW, "available".
            if not loader.getInstalled():
                return True
        return False

    def is_package_upgrade(self, package):
        """Is the package an upgrade for another installed package?"""
        is_upgrade = False
        for upgrade in package.upgrades:
            for provides in upgrade.providedby:
                for provides_package in provides.packages:
                    if provides_package.installed:
                        is_upgrade = True
                        break
                else:
                    continue
                break
            else:
                continue
            break
        return is_upgrade
