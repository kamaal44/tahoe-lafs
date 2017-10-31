
import os, sys
from allmydata.scripts.common import BasedirOptions
from twisted.scripts import twistd
from twisted.python import usage
from twisted.python.reflect import namedAny
from allmydata.scripts.default_nodedir import _default_nodedir
from allmydata.util import fileutil
from allmydata.util.encodingutil import listdir_unicode, quote_local_unicode_path
from twisted.application.service import Service


def identify_node_type(basedir):
    """
    :return unicode: None or one of: 'client', 'introducer',
        'key-generator' or 'stats-gatherer'
    """
    tac = u''
    try:
        for fn in listdir_unicode(basedir):
            if fn.endswith(u".tac"):
                tac = fn
                break
    except OSError:
        return None

    for t in (u"client", u"introducer", u"key-generator", u"stats-gatherer"):
        if t in tac:
            return t
    return None


class DaemonizeOptions(BasedirOptions):
    subcommand_name = "start"
    optParameters = [
        ("basedir", "C", None,
         "Specify which Tahoe base directory should be used."
         " This has the same effect as the global --node-directory option."
         " [default: %s]" % quote_local_unicode_path(_default_nodedir)),
        ]

    def parseArgs(self, basedir=None, *twistd_args):
        # This can't handle e.g. 'tahoe start --nodaemon', since '--nodaemon'
        # looks like an option to the tahoe subcommand, not to twistd. So you
        # can either use 'tahoe start' or 'tahoe start NODEDIR
        # --TWISTD-OPTIONS'. Note that 'tahoe --node-directory=NODEDIR start
        # --TWISTD-OPTIONS' also isn't allowed, unfortunately.

        BasedirOptions.parseArgs(self, basedir)
        self.twistd_args = twistd_args

    def getSynopsis(self):
        return ("Usage:  %s [global-options] %s [options]"
                " [NODEDIR [twistd-options]]"
                % (self.command_name, self.subcommand_name))

    def getUsage(self, width=None):
        t = BasedirOptions.getUsage(self, width) + "\n"
        twistd_options = str(MyTwistdConfig()).partition("\n")[2].partition("\n\n")[0]
        t += twistd_options.replace("Options:", "twistd-options:", 1)
        t += """

Note that if any twistd-options are used, NODEDIR must be specified explicitly
(not by default or using -C/--basedir or -d/--node-directory), and followed by
the twistd-options.
"""
        return t


class MyTwistdConfig(twistd.ServerOptions):
    subCommands = [("DaemonizeTahoeNode", None, usage.Options, "node")]


class DaemonizeTheRealService(Service):

    def __init__(self, nodetype, basedir, options):
        self.nodetype = nodetype
        self.basedir = basedir

    def startService(self):

        def key_generator_removed():
            raise ValueError("key-generator support removed, see #2783")

        def start():
            node_to_instance = {
                u"client": lambda: namedAny("allmydata.client.Client")(self.basedir),
                u"introducer": lambda: namedAny("allmydata.introducer.server.IntroducerNode")(self.basedir),
                u"stats-gatherer": lambda: namedAny("allmydata.stats.StatsGathererService")(self.basedir, verbose=True),
                u"key-generator": key_generator_removed,
            }

            try:
                service_factory = node_to_instance[self.nodetype]
            except KeyError:
                raise ValueError("unknown nodetype %s" % self.nodetype)

            srv = service_factory()
            srv.setServiceParent(self.parent)

        from twisted.internet import reactor
        reactor.callWhenRunning(start)


class DaemonizeTahoeNodePlugin(object):
    tapname = "tahoenode"
    def __init__(self, nodetype, basedir):
        self.nodetype = nodetype
        self.basedir = basedir

    def makeService(self, so):
        return DaemonizeTheRealService(self.nodetype, self.basedir, so)


def daemonize(config):
    """
    Runs the 'tahoe daemonize' command.

    Sets up the IService instance corresponding to the type of node
    that's starting and uses Twisted's twistd runner to disconnect our
    process from the terminal.
    """
    out = config.stdout
    err = config.stderr
    basedir = config['basedir']
    quoted_basedir = quote_local_unicode_path(basedir)
    print >>out, "daemonizing in {}".format(quoted_basedir)
    if not os.path.isdir(basedir):
        print >>err, "%s does not look like a directory at all" % quoted_basedir
        return 1
    nodetype = identify_node_type(basedir)
    if not nodetype:
        print >>err, "%s is not a recognizable node directory" % quoted_basedir
        return 1
    # Now prepare to turn into a twistd process. This os.chdir is the point
    # of no return.
    os.chdir(basedir)
    twistd_args = []
    if (nodetype in (u"client", u"introducer")
        and "--nodaemon" not in config.twistd_args
        and "--syslog" not in config.twistd_args
        and "--logfile" not in config.twistd_args):
        fileutil.make_dirs(os.path.join(basedir, u"logs"))
        twistd_args.extend(["--logfile", os.path.join("logs", "twistd.log")])
    twistd_args.extend(config.twistd_args)
    twistd_args.append("DaemonizeTahoeNode") # point at our DaemonizeTahoeNodePlugin

    twistd_config = MyTwistdConfig()
    try:
        twistd_config.parseOptions(twistd_args)
    except usage.error, ue:
        # these arguments were unsuitable for 'twistd'
        print >>err, config
        print >>err, "tahoe %s: usage error from twistd: %s\n" % (config.subcommand_name, ue)
        return 1
    twistd_config.loadedPlugins = {"DaemonizeTahoeNode": DaemonizeTahoeNodePlugin(nodetype, basedir)}

    # On Unix-like platforms:
    #   Unless --nodaemon was provided, the twistd.runApp() below spawns off a
    #   child process, and the parent calls os._exit(0), so there's no way for
    #   us to get control afterwards, even with 'except SystemExit'. If
    #   application setup fails (e.g. ImportError), runApp() will raise an
    #   exception.
    #
    #   So if we wanted to do anything with the running child, we'd have two
    #   options:
    #
    #    * fork first, and have our child wait for the runApp() child to get
    #      running. (note: just fork(). This is easier than fork+exec, since we
    #      don't have to get PATH and PYTHONPATH set up, since we're not
    #      starting a *different* process, just cloning a new instance of the
    #      current process)
    #    * or have the user run a separate command some time after this one
    #      exits.
    #
    #   For Tahoe, we don't need to do anything with the child, so we can just
    #   let it exit.
    #
    # On Windows:
    #   twistd does not fork; it just runs in the current process whether or not
    #   --nodaemon is specified. (As on Unix, --nodaemon does have the side effect
    #   of causing us to log to stdout/stderr.)

    if "--nodaemon" in twistd_args or sys.platform == "win32":
        verb = "running"
    else:
        verb = "starting"

    print >>out, "%s node in %s" % (verb, quoted_basedir)
    twistd.runApp(twistd_config)
    # we should only reach here if --nodaemon or equivalent was used
    return 0