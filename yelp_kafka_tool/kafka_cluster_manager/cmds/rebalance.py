import logging
import sys

from .command import ClusterManagerCmd
from yelp_kafka_tool.kafka_cluster_manager. \
    cluster_info.cluster_topology import ClusterTopology
from yelp_kafka_tool.kafka_cluster_manager. \
    cluster_info.stats import imbalance_value_all
from yelp_kafka_tool.kafka_cluster_manager. \
    cluster_info.util import validate_plan
from yelp_kafka_tool.kafka_cluster_manager.util import assignment_to_plan
from yelp_kafka_tool.util.zookeeper import ZK


DEFAULT_MAX_PARTITION_MOVEMENTS = 1
DEFAULT_MAX_LEADER_CHANGES = 5


class RebalanceCmd(ClusterManagerCmd):

    def __init__(self):
        super(RebalanceCmd, self).__init__()
        self.log = logging.getLogger('ClusterRebalance')

    def add_subparser(self, subparsers):
        subparser = subparsers.add_parser(
            'rebalance',
            description='Re-assign partitions over brokers.',
        )
        subparser.add_argument(
            '--replication-groups',
            action='store_true',
            help='Evenly distributes replicas over replication-groups.',
        )
        subparser.add_argument(
            '--brokers',
            action='store_true',
            help='Evenly distributes partitions optimally over brokers'
            ' with minimal movements for each replication-group.',
        )
        subparser.add_argument(
            '--leaders',
            action='store_true',
            help='Evenly distributes leaders optimally over brokers.',
        )
        subparser.add_argument(
            '--max-partition-movements',
            type=self.positive_int,
            default=DEFAULT_MAX_PARTITION_MOVEMENTS,
            help='Maximum number of partition-movements in final set of actions.'
                 ' DEFAULT: %(default)s. RECOMMENDATION: Should be at least max '
                 'replication-factor across the cluster.',
        )
        subparser.add_argument(
            '--max-leader-changes',
            type=self.positive_int,
            default=DEFAULT_MAX_LEADER_CHANGES,
            help='Maximum number of actions with leader-only changes.'
                 ' DEFAULT: %(default)s',
        )
        subparser.set_defaults(command=self.rebalance_cluster)

    def rebalance_cluster(self, cluster_config, args):
        """Get executable proposed plan(if any) for display or execution."""
        with ZK(cluster_config) as zk:
            self.log.debug(
                'Starting re-assignment tool for cluster: {c_name} and zookeeper: '
                '{zookeeper}'.format(
                    c_name=cluster_config.name,
                    zookeeper=cluster_config.zookeeper,
                )
            )
            # Exit if there is an on-going reassignment
            if self.is_reassignment_pending(zk):
                self.log.error('Previous reassignment pending. Exiting...')
                sys.exit(1)

            ct = ClusterTopology(zk=zk)
            # TODO: We could get rid of initial_assignment in ClusterTopology
            base_assignment = ct.initial_assignment

            assignment = self.build_balanced_assignment(ct, args)

            # TODO: Get rid of validate_plan in the future. We should
            # assume that the rebalance is safe and it doesn't need any extra
            # validation.
            self.log.info('Validating current cluster-topology against initial cluster-topology...')
            if not validate_plan(assignment_to_plan(assignment), assignment_to_plan(base_assignment)):
                self.log.error('Invalid latest-cluster assignment. Exiting...')
                sys.exit(1)

            # Reduce the proposed assignment based on max_partition_movements
            # and max_leader_changes
            reduced_assignment, total_changes = self.get_reduced_assignment(
                base_assignment,
                assignment,
                args.max_partition_movements,
                args.max_leader_changes,
            )
            self.log.info("Total changes before reduction: {0}".format(total_changes))
            # Display or store plan
            # TODO: temporary disable this function
            # display_assignment_changes(assignment)
            # Export proposed-plan to json file
            plan = assignment_to_plan(reduced_assignment)
            if args.proposed_plan_file:
                self.log.info(
                    'Storing proposed-plan in json file, {file}'
                    .format(file=args.proposed_plan_file),
                )
                self.write_json_plan(plan, args.proposed_plan_file)

            self.log.info(
                'Proposed plan assignment {plan}'
                .format(plan=plan),
            )
            self.log.info(
                'Proposed-plan actions count: {actions}, '.format(
                    actions=len(plan['partitions']),
                ),
            )
            self.execute_plan(zk, plan, args.apply, args.no_confirm)

    def build_balanced_assignment(self, ct, args):
        # Get initial imbalance statistics
        initial_imbal = self.pre_balancing_imbalance_stats(ct)

        # Balancing to be done in the given order only
        # Rebalance replication-groups
        if args.replication_groups:
            self.log.info(
                'Re-balancing replica-count over replication groups: {groups}...'
                .format(groups=', '.join(ct.rgs.keys())),
            )
            ct.rebalance_replication_groups()
            self.replication_group_rebalance_stats(ct)

        # Rebalance broker-partition count per replication-groups
        if args.brokers:
            self.log.info(
                'Re-balancing partition-count across brokers: {brokers}...'
                .format(brokers=', '.join(str(e) for e in ct.brokers.keys())),
            )
            ct.rebalance_brokers()
            self.broker_rebalance_stats(ct, initial_imbal)

        # Rebalance broker as leader count per broker
        if args.leaders:
            self.log.info(
                'Re-balancing leader-count across brokers: {brokers}...'
                .format(brokers=', '.join(str(e) for e in ct.brokers.keys())),
            )
            ct.rebalance_leaders()

            self.final_rebalance_stats(ct, initial_imbal, args.leaders)

        return ct.assignment

    # Imbalance statistics evaluation and reporting
    def pre_balancing_imbalance_stats(self, ct):
        self.log.info('Calculating initial rebalance imbalance statistics...')
        initial_imbal = imbalance_value_all(ct)
        self.log_imbalance_stats(initial_imbal)
        total_imbal = (
            initial_imbal['replica_cnt'] +
            initial_imbal['net_part_cnt_per_rg'] +
            initial_imbal['topic_partition_cnt'] +
            initial_imbal['partition_cnt'] +
            initial_imbal['leader_cnt']
        )
        if total_imbal == 0:
            self.log.info('Cluster is currently balanced!')
        return initial_imbal

    def replication_group_rebalance_stats(self, ct):
        self.log.info(
            'Calculating rebalance imbalance-stats after rebalancing '
            'replica-count over replication-groups...',
        )
        curr_imbal = imbalance_value_all(ct, leaders=False)
        self.log.info(
            'Imbalance statistics after rebalancing replica-count over '
            'replication-groups'
        )
        self.log_imbalance_stats(curr_imbal, leaders=False)
        # Assert that replication-groups balanced
        assert(curr_imbal['replica_cnt'] == 0), (
            'Replication-group imbalance count is non-zero: {imbal}'
            .format(imbal=curr_imbal['replica_cnt']),
        )

    def broker_rebalance_stats(self, ct, initial_imbal):
        self.log.info(
            'Calculating rebalance imbalance-stats after rebalancing brokers...',
        )
        curr_imbal = imbalance_value_all(ct, leaders=False)
        self.log_imbalance_stats(curr_imbal, leaders=False)
        if curr_imbal['net_part_cnt_per_rg'] > 0:
            # Report as warning if replication-groups didn't rebalance
            self.log.error(
                'Partition-count over brokers imbalance count is non-zero: '
                '{imbal}'.format(imbal=curr_imbal['net_part_cnt_per_rg']),
            )
        # Assert that replication-group imbalance should not increase
        assert(
            curr_imbal['net_part_cnt_per_rg'] <=
            initial_imbal['net_part_cnt_per_rg']), (
            'Partition-count imbalance count increased from '
            '{initial_imbal} to {curr_imbal}'.format(
                initial_imbal=initial_imbal['net_part_cnt_per_rg'],
                curr_imbal=curr_imbal['net_part_cnt_per_rg'],
            )
        )

    def final_rebalance_stats(self, ct, initial_imbal, leaders_balanced=False):
        self.log.info('Calculating final rebalance imbalance-stats... ')
        curr_imbal = imbalance_value_all(ct)
        self.log_imbalance_stats(curr_imbal)
        # Verify leader-imbalance only if balanced
        if leaders_balanced:
            if curr_imbal['leader_cnt'] > 0:
                # Report as warning if replication-groups didn't rebalance
                ct.log.warning(
                    'Leader-count over brokers imbalance count is non-zero: '
                    '{imbal}'.format(imbal=curr_imbal['leader_cnt']),
                )
            # Assert that leader-imbalance should not increase
            assert(curr_imbal['leader_cnt'] <= initial_imbal['leader_cnt']), (
                'Leader-count imbalance count increased from '
                '{initial_imbal} to {curr_imbal}'.format(
                    initial_imbal=initial_imbal['leader_cnt'],
                    curr_imbal=curr_imbal['leader_cnt'],
                )
            )

    def log_imbalance_stats(self, imbal, leaders=True):
        net_imbalance = (
            imbal['replica_cnt'] +
            imbal['net_part_cnt_per_rg'] +
            imbal['topic_partition_cnt']
        )
        self.log.info(
            'Replication-group imbalance (replica-count): {imbal_repl}\n'
            'Net Partition-count imbalance/replication-group: '
            '{imbal_part_rg}\nNet Partition-count imbalance: {imbal_part}\n'
            'Topic-partition-count imbalance: {imbal_tp}\n'
            'Net-cluster imbalance (excluding leader-imbalance): '
            '{imbal_net}'.format(
                imbal_part_rg=imbal['net_part_cnt_per_rg'],
                imbal_repl=imbal['replica_cnt'],
                imbal_part=imbal['partition_cnt'],
                imbal_tp=imbal['topic_partition_cnt'],
                imbal_net=net_imbalance,
            )
        )
        if leaders:
            net_imbalance_with_leaders = net_imbalance + imbal['leader_cnt']
            self.log.info(
                'Leader-count imbalance: {imbal_leader}\n'
                'Net-cluster imbalance (including leader-imbalance): '
                '{imbal}'.format(
                    imbal=net_imbalance_with_leaders,
                    imbal_leader=imbal['leader_cnt'],
                )
            )
        self.log.info(
            'Total partition-movements: {movement_cnt}'
            .format(movement_cnt=imbal['total_movements']),
        )
