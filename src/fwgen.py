#!/usr/bin/env python3

import argparse
import sys
import re
import subprocess

import yaml


DEFAULT_CHAINS_IP = {
    'filter': ['INPUT', 'FORWARD', 'OUTPUT'],
    'nat': ['PREROUTING', 'INPUT', 'OUTPUT', 'POSTROUTING'],
    'mangle': ['PREROUTING', 'INPUT', 'FORWARD', 'OUTPUT', 'POSTROUTING'],
    'raw': ['PREROUTING', 'OUTPUT'],
    'security': ['INPUT', 'FORWARD', 'OUTPUT']
}
DEFAULT_CHAINS_IP6 = DEFAULT_CHAINS_IP
CONFIG = '/etc/fwgen/config.yml'
DEFAULTS = '/etc/fwgen/defaults.yml'
IPTABLES_RESTORE = '/etc/iptables.restore'
IP6TABLES_RESTORE = '/etc/ip6tables.restore'
IPSETS_RESTORE = '/etc/ipsets.restore'


class FwGen(object):
    def __init__(self, config):
        self.config = config
        self.default_chains = {
            'ip': DEFAULT_CHAINS_IP,
            'ip6': DEFAULT_CHAINS_IP6
        }

    def output_ipsets(self, reset=False):
        ipset_family_map = {
            'ip': 'inet',
            'ip6': 'inet6'
        }
        list_type = {
            'networks': 'hash:net',
            'hosts': 'hash:ip'
        }

        if reset:
            yield 'flush'
            yield 'destroy'
        else:
            for group_type, names in self.config.get('groups', {}).items():
                for name, families in names.items():
                    yield '-exist create %s list:set' % name
                    yield 'flush %s' % name

                    for family, entries in families.items():
                        family_set = '%s_%s' % (name, family)

                        yield '-exist create %s %s family %s' % (family_set,
                                                                 list_type[group_type],
                                                                 ipset_family_map[family])
                        yield 'add %s %s' % (name, family_set)
                        yield 'flush %s' % family_set

                        for entry in entries:
                            yield 'add %s %s' % (family_set, entry)

    def get_policy_rules(self, family, reset=False):
        for table, chains in self.default_chains[family].items():
            for chain in chains:
                policy = 'ACCEPT'

                if not reset:
                    try:
                        policy = self.config['policies'][family][table][chain]
                    except KeyError:
                        pass

                yield (table, ':%s %s' % (chain, policy))

    def get_zone_rules(self, family):
        for zone, params in self.config['zones'].items():
            try:
                for table, chains in params['rules'][family].items():
                    for chain, chain_rules in chains.items():
                        zone_chain = '%s_%s' % (zone, chain)
                        for rule in chain_rules:
                            yield (table, '-A %s %s' % (zone_chain, rule))
            except KeyError:
                continue

    def get_default_rules(self, family):
        try:
            rules = self.config['defaults']['rules'][family]
        except KeyError:
            rules = {}
        return self.get_rules(rules)

    def get_helper_chains(self, family):
        try:
            rules = self.config['helper_chains'][family]
        except KeyError:
            rules = {}

        for table, chains in rules.items():
            for chain in chains:
                yield self.get_new_chain_rule(table, chain)

        yield from self.get_rules(rules)

    @staticmethod
    def get_rules(rules):
        for table, chains in rules.items():
            for chain, chain_rules in chains.items():
                for rule in chain_rules:
                    yield (table, '-A %s %s' % (chain, rule))

    @staticmethod
    def get_new_chain_rule(table, chain):
        return (table, ':%s -' % chain)

    def get_zone_dispatchers(self, family):
        for zone, params in self.config['zones'].items():
            try:
                for table, chains in params['rules'][family].items():
                    for chain in chains:
                        dispatcher_chain = '%s_%s' % (zone, chain)
                        yield self.get_new_chain_rule(table, dispatcher_chain)

                        if chain in ['PREROUTING', 'INPUT', 'FORWARD']:
                            yield (table, '-A %s -i %%{%s} -j %s' % (chain, zone, dispatcher_chain))
                        elif chain in ['OUTPUT', 'POSTROUTING']:
                            yield (table, '-A %s -o %%{%s} -j %s' % (chain, zone, dispatcher_chain))
                        else:
                            raise Exception('%s is not a valid default chain' % chain)
            except KeyError:
                continue

    def expand_zones(self, rule):
        zone_pattern = re.compile(r'^(.+?\s)%\{(.+?)\}(\s.+)$')
        match = re.search(zone_pattern, rule)

        if match:
            zone = match.group(2)

            for interface in self.config['zones'][zone]['interfaces']:
                rule_expanded = '%s%s%s' % (match.group(1), interface, match.group(3))
                yield from self.expand_zones(rule_expanded)
        else:
            yield rule

    def output_rules(self, rules, family):
        for table in self.default_chains[family]:
            yield '*%s' % table

            for rule_table, rule in rules:
                if rule_table == table:
                    yield from self.expand_zones(rule)

            yield 'COMMIT'

    def save_ipsets(self, path):
        """
        Avoid using `ipset save` in case there are other
        ipsets used on the system for other purposes. Also
        this avoid storing now unused ipsets from previous
        configurations.
        """
        with open(path, 'w') as f:
            for item in self.output_ipsets():
                f.write('%s\n' % item)

    @staticmethod
    def save_rules(path, family):
        cmd = {
            'ip': ['iptables-save'],
            'ip6': ['ip6tables-save']
        }

        with open(path, 'wb') as f:
            subprocess.run(cmd[family], stdout=f)

    def save(self):
        save = {
            'ip': IPTABLES_RESTORE,
            'ip6': IP6TABLES_RESTORE
        }

        for family in ['ip', 'ip6']:
            self.save_rules(save[family], family)

        self.save_ipsets(IPSETS_RESTORE)

    @staticmethod
    def apply_rules(rules, family):
        cmd = {
            'ip': ['iptables-restore'],
            'ip6': ['ip6tables-restore']
        }
        stdin = ('%s\n' % '\n'.join(rules)).encode('utf-8')
        subprocess.run(cmd[family], input=stdin)

    @staticmethod
    def apply_ipsets(ipsets):
        stdin = ('%s\n' % '\n'.join(ipsets)).encode('utf-8')
        subprocess.run(['ipset', 'restore'], input=stdin)

    def apply(self):
        # Apply ipsets first to ensure they exist when the rules are applied
        self.apply_ipsets(self.output_ipsets())

        for family in ['ip', 'ip6']:
            rules = []
            rules.extend(self.get_policy_rules(family))
            rules.extend(self.get_default_rules(family))
            rules.extend(self.get_helper_chains(family))
            rules.extend(self.get_zone_dispatchers(family))
            rules.extend(self.get_zone_rules(family))
            self.apply_rules(self.output_rules(rules, family), family)

    def commit(self):
        self.apply()
        self.save()

    def reset(self):
        for family in ['ip', 'ip6']:
            rules = []
            rules.extend(self.get_policy_rules(family, reset=True))
            self.apply_rules(self.output_rules(rules, family), family)

        # Reset ipsets after the rules are removed to ensure ipsets are not in use
        self.apply_ipsets(self.output_ipsets(reset=True))


def dict_merge(d1, d2):
    """
    Deep merge d1 into d2
    """
    for k, v in d1.items():
        if isinstance(v, dict):
            node = d2.setdefault(k, {})
            dict_merge(v, node)
        else:
            d2[k] = v

    return d2

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', metavar='PATH', help='Path to config file')
    parser.add_argument('--with-reset', action='store_true',
        help='Clear the firewall before reapplying. Recommended only if ipsets in '
             'use are preventing you from applying the new configuration.')
    args = parser.parse_args()

    user_config = CONFIG
    defaults = DEFAULTS

    if args.config:
        user_config = args.config

    with open(defaults, 'r') as f:
        config = yaml.load(f)

    with open(user_config, 'r') as f:
        config = dict_merge(yaml.load(f), config)

    fw = FwGen(config)
    if args.with_reset:
        fw.reset()
    fw.commit()

if __name__ == '__main__':
    sys.exit(main())
