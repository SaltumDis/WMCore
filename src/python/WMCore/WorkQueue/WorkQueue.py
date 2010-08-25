#!/usr/bin/env python
"""
WorkQueue provides functionality to queue large chunks of work,
thus acting as a buffer for the next steps in job processing

WMSpec objects are fed into the queue, split into coarse grained work units
and released when a suitable resource is found to execute them.

https://twiki.cern.ch/twiki/bin/view/CMS/WMCoreJobPool
"""

__revision__ = "$Id: WorkQueue.py,v 1.43 2009/12/14 13:50:27 swakef Exp $"
__version__ = "$Revision: 1.43 $"

# pylint: disable-msg = W0104, W0622
try:
    set
except NameError:
    from sets import Set as set
# pylint: enable-msg = W0104, W0622

import uuid
import time
import os
try:
    from collections import defaultdict    #python2.6 doesn't work fully yet
except (NameError, ImportError):
    pass

from WMCore.Services.DBS.DBSReader import DBSReader
from WMCore.Services.PhEDEx.PhEDEx import PhEDEx

from WMCore.WorkQueue.WorkQueueBase import WorkQueueBase
from WMCore.WorkQueue.WMBSHelper import WMBSHelper
from WMCore.WorkQueue.Policy.Start import startPolicy
from WMCore.WorkQueue.Policy.End import endPolicy

from WMCore.WMSpec.WMWorkload import WMWorkloadHelper, getWorkloadFromTask
from WMCore.WorkQueue.WorkSpecTaskParser import WorkSpecTaskParser
from WMCore.WMBS.Subscription import Subscription as WMBSSubscription
from WMCore.WMBS.File import File as WMBSFile

#TODO: Scale test
#TODO: Handle multiple dbs instances
#TODO: Decide whether to move/refactor db functions
#TODO: Transaction handling
#TODO: What about sending messages to component to handle almost live status updates

#  //
# // Convenience constructor functions
#//
def globalQueue(logger = None, dbi = None, **kwargs):
    """Convenience method to create a WorkQueue suitable for use globally
    """
    defaults = {'SplitByBlock' : False,
                'PopulateFilesets' : False,
                'SplittingMapping' : {'DatasetBlock' : ('Dataset', {})}
                }
    defaults.update(kwargs)
    return WorkQueue(logger, dbi, **defaults)

def localQueue(logger = None, dbi = None, **kwargs):
    """Convenience method to create a WorkQueue suitable for use locally
    """
    defaults = {'TrackLocationOrSubscription' : 'location'}
    defaults.update(kwargs)
    return WorkQueue(logger, dbi, **defaults)



class WorkQueue(WorkQueueBase):
    """
    _WorkQueue_

    collection of work queue elements,

    This  provide API for JSM (WorkQueuePool) - getWork(), gotWork()
    and injector
    """
    def __init__(self, logger = None, dbi = None, **params):
        WorkQueueBase.__init__(self, logger, dbi)
        self.dbsHelpers = {}
        self.remote_queues = {}
        self.lastLocationUpdate = 0
        self.lastReportToParent = 0
        self.lastFullReportToParent = 0
        self.parent_queue = None
        self.params = params
        self.params.setdefault('ParentQueue', None) # Get more work from here
        self.params.setdefault('QueueDepth', 2) # when less than this locally
        self.params.setdefault('SplitByBlock', True)
        self.params.setdefault('ItemWeight', 0.01) # Queuing time weighted avg
        self.params.setdefault('FullLocationRefreshInterval', 3600)
        self.params.setdefault('TrackLocationOrSubscription', 'subscription')
        self.params.setdefault('ReleaseIncompleteBlocks', False)
        self.params.setdefault('ReleaseRequireSubscribed', True)
        self.params.setdefault('PhEDExEndpoint', None)
        self.params.setdefault('PopulateFilesets', True)
        self.params.setdefault('CacheDir', os.path.join(os.getcwd(),
                                                        'wf_cache'))
        self.params.setdefault('NegotiationTimeout', 3600)
        self.params.setdefault('QueueURL', None) # url this queue is visible on
        self.params.setdefault('FullReportInterval', 3600)
        self.params.setdefault('ReportInterval', 300)

        self.params.setdefault('SplittingMapping', {})
        self.params['SplittingMapping'].setdefault('DatasetBlock', ('Block', {}))
        self.params['SplittingMapping'].setdefault('MonteCarlo', ('MonteCarlo', {}))
        self.params.setdefault('EndPolicySettings', {})

        assert(self.params['TrackLocationOrSubscription'] in ('subscription',
                                                              'location'))
        # Can only release blocks on location
        if self.params['TrackLocationOrSubscription'] == 'location':
            if not self.params['SplitByBlock']:
                raise RuntimeError, 'Only blocks can be released on location'

        phedexArgs = {}
        if self.params.get('PhEDExEndpoint'):
            phedexArgs['endpoint'] = self.params['PhEDExEndpoint']
        self.phedexService = PhEDEx(phedexArgs)

        if self.params['CacheDir']:
            try:
                os.makedirs(self.params['CacheDir'])
            except OSError:
                pass

        if self.params['ParentQueue'] is not None and not self.params['QueueURL']:
            raise RuntimeError, "ParentQueue defined but not QueueURL"
        if self.params['ParentQueue'] is not None:
            self.parent_queue = self._get_remote_queue(self.params['ParentQueue'])


    #  //
    # // External API
    #//

    def __len__(self):
        """Returns number of Available elements in queue"""
        items = self.daofactory(classname = "WorkQueueElement.CountElements")
        return items.execute('Available', conn = self.getDBConn(),
                                 transaction = self.existingTransaction())

    def setStatus(self, status, ids, id_type = 'id', source = None):
        """
        _setStatus_, throws an exception if no elements are updated

        @param source - where a status update came from (remote queue)
                      - already knows the status so don't send it
        """
        try:
            iter(ids)
        except TypeError:
            ids = [ids]

        updateAction = self.daofactory(classname =
                                       "WorkQueueElement.UpdateStatus")
        affected = updateAction.execute(status, ids, id_type, source,
                                    conn = self.getDBConn(),
                                    transaction = self.existingTransaction())
        if not affected:
            raise RuntimeError, "Status not changed: No matching elements"

        #TODO: Do we need to message parents/children here?
        # Would be quicker than waiting for the next status updates
        # but would need listening services and firewall holes etc.


    def setPriority(self, newpriority, *workflowNames):
        """
        Update priority for a workflow, throw exception if no elements affected
        """
        updateAction = self.daofactory(classname = "WorkQueueElement.UpdatePriority")
        affected = updateAction.execute(newpriority, workflowNames,
                             conn = self.getDBConn(),
                             transaction = self.existingTransaction())
        if not affected:
            raise RuntimeError, "Priority not changed: No matching elements"

    def getWork(self, siteJobs, pullingQueueUrl = None):
        """ 
        _getWork_
        siteJob is dict format of {site: estimateJobSlot}
        
        JobCreator calls this method, it will 
        1. match jobs with work queue element
        2. create the subscription for it if it is not already exist. 
           (currently set to have one subscription per a workload)
           (associate the subscription to workload - currently following naming convention,
            so it can retrieved by workflow name - but might needs association table)
        3. fill up the fileset with files in the subscription 
           when if it is processing jobs. if it is production jobs (MC) fileset will be empty
        4. TODO: close the fileset if the last workqueue element of the workload is processed. 
        5. update the workqueue status to ('Acquired') might need finer status change 
           if it fails to create wmbs files partially
        6. return list of subscription (or not)
           it can be only tracked only subscription (workload) level job done
           or
           return workquue element list:
           if we want to track partial level of success. But requires JobCreate map workqueue element
           to each jobgroup. also doneWork parameter should be list of workqueue element not list 
           of subscription
        """
        results = []
        subResults = []
        matches, unmatched = self._match(siteJobs)

        # if talking to a child and have resources left get work from parent
        if pullingQueueUrl and unmatched:
            try:
                if self.pullWork(unmatched):
                    matches, _ = self._match(siteJobs)
            except RuntimeError:
                # log failure to contact parent queue
                pass
        wmSpecInfoAction = self.daofactory(classname = "WMSpec.GetWMSpecInfo")

        for match in matches:
            wmSpecInfo = wmSpecInfoAction.execute(match['wmtask_id'],
                                    conn = self.getDBConn(),
                                    transaction = self.existingTransaction())
            trans = self.beginTransaction()
            if self.params['PopulateFilesets']:

                sub = self._wmbsPreparation(match, wmSpecInfo)

                self.setStatus('Acquired', match['id'], 'id', pullingQueueUrl)

            else:
                status = pullingQueueUrl and 'Negotiating' or 'Acquired'
                self.setStatus(status, match['id'], 'id', pullingQueueUrl)
            self.commitTransaction(trans)

            wmSpecInfo['element_id'] = match['id']
            results.append(wmSpecInfo)

        return results

    def _wmbsPreparation(self, match, wmSpecInfo):
        """
        """
        wAction = self.daofactory(classname = "Site.GetWhiteListByElement")
        whitelist = wAction.execute(match['id'], conn = self.getDBConn(),
                                     transaction = self.existingTransaction())

        bAction = self.daofactory(classname = "Site.GetBlackListByElement")
        blacklist = bAction.execute(match['id'], conn = self.getDBConn(),
                                     transaction = self.existingTransaction())

        if not match['input_id']:
            wmbsHelper = WMBSHelper(wmSpecInfo['wmspec_name'], wmSpecInfo['url'],
                                    wmSpecInfo['owner'], wmSpecInfo['wmtask_name'],
                                    whitelist, blacklist, None)
            sub = wmbsHelper.createSubscription()
        else:
            #TODO : not to create dao multiple times
            blockLoader = self.daofactory(classname = "Data.LoadByID")
            #TODO: need to populate parent block
            #parentBlockLoader = \
            #        self.daofactory(classname = "Data.GetParentsByChildID")

            block = blockLoader.execute(match['input_id'],
                                    conn = self.getDBConn(),
                                    transaction = self.existingTransaction())

            wmbsHelper = WMBSHelper(wmSpecInfo['wmspec_name'], wmSpecInfo['url'],
                                    wmSpecInfo['owner'], wmSpecInfo['wmtask_name'],
                                    whitelist, blacklist, block['name'])
            sub = wmbsHelper.createSubscription()

            dbs = self.dbsHelpers.values()[0] #FIXME!!!
            if match['parent_flag']:
                dbsBlock = dbs.getFileBlockWithParents(block["name"])[block['name']]
            else:
                dbsBlock = dbs.getFileBlock(block["name"])[block['name']]

            # TODO: parent fileset
            fileset = sub["fileset"]

            for dbsFile in dbsBlock['Files']:
                checksums = {}
                if dbsFile.get('Checksum'):
                    checksums['cksum'] = dbsFile['Checksum']
                if dbsFile.get('Adler32'):
                    checksums['adler32'] = dbsFile['Adler32']
                wmbsFile = WMBSFile(lfn = dbsFile["LogicalFileName"],
                        size = dbsFile["FileSize"],
                        events = dbsFile["NumberOfEvents"],
                        checksums = checksums,
                        parents = dbsFile["ParentList"],
                        locations = set(dbsBlock['StorageElements']))
                fileset.addFile(wmbsFile)

            fileset.commit()
            updateSub = self.daofactory(classname = "WorkQueueElement.UpdateSubscription")
            updateSub.execute(match['id'], sub['id'],
                                    conn = self.getDBConn(),
                                    transaction = self.existingTransaction())

            return sub

    def doneWork(self, *subscriptions):
        """
        _doneWork_

        this is called by JSM
        update the WorkQueue status table
        """
        self.setStatus('Done', subscriptions)


    def failWork(self, *subscriptions):
        """Mark work as failed"""
        self.setStatus('Failed', subscriptions)


    def cancelWork(self, *subscriptions):
        """Mark work as canceled"""
        self.setStatus('Canceled', subscriptions)

    def gotWork(self, *subscriptions):
        """
        _gotWork_

        this is called by JSM
        update the WorkQueue status table and remove from further consideration
        """
        self.setStatus('Acquired', *subscriptions)


    def successWork(self, *subscriptions):
        """
        _successWork_

        this is called by JSM
        update the WorkQueue status table
        """
        self.setStatus('Success', *subscriptions)

    def deleteWork(self, *subscriptions):
        """
        _deleteWork_

        this is called by JSM
        update the WorkQueue status table
        """
        pass

    def queueWork(self, wmspecUrl, parentQueueId = None):
        """
        Take and queue work from a WMSpec
        """
        wmspec = WMWorkloadHelper()
        wmspec.load(wmspecUrl)
        units = []
        for topLevelTask in wmspec.taskIterator():
            dbs_url = topLevelTask.dbsUrl()
            wmspec = getWorkloadFromTask(topLevelTask)

            if dbs_url and not self.dbsHelpers.has_key(dbs_url):
                self.dbsHelpers[dbs_url] = DBSReader(dbs_url)

            policy = startPolicy(wmspec.startPolicy(),
                                 self.params['SplittingMapping'])

            units.extend(policy(wmspec, self.dbsHelpers))

        trans = self.beginTransaction()
        for unit in units:
            primaryBlock = unit['Data']
            blocks = unit['ParentData']
            jobs = unit['Jobs']
            wmspec = unit['WMSpec']
            unique = uuid.uuid4().hex[:10] # hopefully random enough
            new_url = os.path.join(self.params['CacheDir'],
                                   "%s.spec" % unique)
            if os.path.exists(new_url):
                raise RuntimeErorr, "spec file %s exists" % new_url
            wmspec.setSpecUrl(new_url) #TODO: look at making this a web accessible url
            wmspec.save(new_url)

            self._insertWorkQueueElement(wmspec, jobs, primaryBlock,
                                         blocks, parentQueueId,
                                         topLevelTask)
        self.commitTransaction(trans)
        return len(units)


    def status(self, status = None, subs = None, before = None, after = None,
               dictKey = None):
        """Return status of elements
           if given only return elements updated since the given time
        """
        action = self.daofactory(classname = "WorkQueueElement.GetElements")
        items = action.execute(since = after,
                              before = before,
                              status = status,
                              subs = None,
                              conn = self.getDBConn(),
                              transaction = self.existingTransaction())
        # if dictKey given format as a dict with the appropriate key
        if dictKey:
            tmp = defaultdict(list)
            for item in items:
                tmp[item[dictKey]].append(item)
            items = tmp
        return items


    def synchronize(self, child_url, child_report):
        """
        Take status from child queue and update ourselves
        """
        my_details = self.status(subs = [x['ParentQueueId'] for x in child_report],
                                 dictKey = "Id")
        #store elements we need to update grouped by status(reduce connections)
        to_update = defaultdict(set)
        # may need to change child status - i.e. if canceled in parent
        child_update = defaultdict(set) # need to be set as have many children

        for item in child_report:
            item_id = item['ParentQueueId']

            # This queue doesn't know about the work - ignore
            if not my_details.has_key(item_id):
                continue

            my_item = my_details[item_id]
            assert(len(my_item) == 1)
            my_item = my_item[0]

            # New in child equates to Acquired in parent
            if item['Status'] == 'Available':
                item['Status'] = 'Acquired'

            # Negotiation failure - Another queue has the work
            if my_item['ChildQueueUrl'] != child_url:
                child_update['Canceled'].add(my_item['Id'])
                continue

            # if status's the same no need to update anything
            if item['Status'] == my_item['Status']:
                continue
            # From here on either this queue or the child needs to be updated

            # if parent in final state (manual intervention?) force child to same state
            if my_item['Status'] in ('Done', 'Failed', 'Canceled'):
                # force child to same state
                child_update[my_item['status']].add(my_item['id'])
                continue

            to_update[item['Status']].add(my_item['Id'])

        trans = self.beginTransaction()
        for status, items in to_update.items():
            self.setStatus(status, items, source = child_url)
        self.commitTransaction(trans)

        # return to the child queue the elements that it needs to update
        return child_update


    def flushNegotiationFailures(self):
        """
        Check for any elements that have been Negotiating for too long,
        and reset them to allow them to be acquired again.
        """
        items = self.daofactory(classname = "WorkQueueElement.GetExpiredElements")
        items = items.execute(conn = self.getDBConn(),
                              status = 'Negotiating',
                              interval = self.params['NegotiationTimeout'],
                              transaction = self.existingTransaction())
        if items:
            # log negotiation failures and setStatus to available
            self.setStatus('Available', [x['id'] for x in items])
        return len(items)






    #  //
    # // Methods that call out to remote services
    #//

    def updateLocationInfo(self):
        """
        Update locations for elements
        """
        trans = self.beginTransaction()
        #get blocks and dbsurls (for now assume global!)
        blocksAction = self.daofactory(classname = "Data.GetActiveData")
        blocks = blocksAction.execute(conn = self.getDBConn(),
                                      transaction = self.existingTransaction())
        if not blocks:
            return

        fullResync = time.time() > self.lastLocationUpdate + \
                                self.params['FullLocationRefreshInterval']

        mapping = self._getLocations([x['name'] for x in blocks], fullResync)

        if not mapping:
            return

        uniqueLocations = set(sum(mapping.values(), []))

        if uniqueLocations:
            self._insertSite(list(uniqueLocations))

        mappingAct = self.daofactory(classname = "Site.UpdateDataSiteMapping")
        mappingAct.execute(mapping, fullResync, conn = self.getDBConn(),
                           transaction = self.existingTransaction())
        self.commitTransaction(trans)

    def pullWork(self, resources):
        """
        Pull work from another WorkQueue to be processed

        If resources passed in get work for them, if not get from wmbs.
        """
        amount = 0
        if self.parent_queue:
            if not resources:
                from WMCore.DAOFactory import DAOFactory
                action = DAOFactory(package = 'WMBS.Database',
                                    logger = self.logger,
                                    dbinterface = self.dbi)('Locations.List')
                wmbs_sites = action.execute(conn = self.getDBConn(),
                                       transaction = self.existingTransaction())
                sites = {}
                # get more work for the future
                [sites.__setitem__(name,
                                   self.params['QueueDepth'] * slots) for _,
                                   name, slots in wmbs_sites]
                _, resources = self.getWork(sites)

            if resources:
                work = self.parent_queue.getWork(resources,
                                             self.params['QueueURL'])
                if work:
                    trans = self.beginTransaction()
                    for element in work:
                        amount += self.queueWork(element['url'],
                                                 element['element_id'])
                    self.commitTransaction(trans)
        return amount

    def updateParent(self, full = False):
        """
        Report status of elements to the parent queue

        Either report status's as provided or get all elements
        """
        if self.parent_queue is None:
            return

        # check whether we need to do a full report
        now = time.time()
        if not full:
            full = self.lastFullReportToParent + \
                            self.params['FullReportInterval'] < now
        if full:
            since = None
        else:
            since = self.lastReportToParent

        # Get queue elements grouped by their parent
        elements = self.status(after = since, dictKey = "ParentQueueId")

        # apply end policy to elements grouped by parent
        items = [endPolicy(group,
                           self.params['EndPolicySettings']) for \
                                                    group in elements.values()]
        if items:
            try:
                # send to remote queue
                # check that we don't have an error from incompatible states
                # i.e. canceled in parent - if so cancel here...
                result = self.parent_queue.synchronize(self.params['QueueURL'],
                                                       items)
            except RuntimeError:
                # log a failure to communicate
                raise

            # some of our element status's may be overriden by the parent
            # e.g. if request is canceled at top level
            if result:
                trans = self.beginTransaction()
                for status, items in result.items():
                    self.setStatus(status, items, id_type = 'parent_queue_id')
                self.commitTransaction(trans)

        if full:
            self.lastFullReportToParent = now
        else:
            self.lastReportToParent = now



    #  //
    # //  Internal methods
    #//

    def _insertWorkQueueElement(self, wmspec, nJobs, primaryInput,
                                parentInputs, parentQueueId, task):
        """
        Persist a block to the database
        """
        self._insertWMSpec(wmspec)
        self._insertWMTask(wmspec.name(), task)

        if primaryInput:
            self._insertInputs(primaryInput, parentInputs)

        wqAction = self.daofactory(classname = "WorkQueueElement.New")
        parentFlag = parentInputs and 1 or 0
        priority = wmspec.priority() or 1

        elementID = wqAction.execute(wmspec.name(), task.name(), primaryInput, nJobs,
                         priority, parentFlag, parentQueueId, conn = self.getDBConn(),
                         transaction = self.existingTransaction())

        whitelist = task.siteWhitelist()
        if len(whitelist) != 0:
            self._insertWhiteList(elementID, whitelist)
        blacklist = task.siteBlacklist()
        if len(blacklist) != 0:
            self._insertBlackList(elementID, blacklist)

    def _insertWMSpec(self, wmSpec):
        """
        """
        #FIXME: Doesn't work - results in differing elements having the same spec url!!!!
        existsAction = self.daofactory(classname = "WMSpec.Exists")
        exists = existsAction.execute(wmSpec.name(), conn = self.getDBConn(),
                             transaction = self.existingTransaction())

        if not exists:
            wmSpecAction = self.daofactory(classname = "WMSpec.New")
            #TODO: need a unique value (name?) for first parameter
            owner = str(wmSpec.owner()) or self.params['QueueURL'] or "WorkQueue"
            wmSpecAction.execute(wmSpec.name(), wmSpec.specUrl(), owner,
                                 conn = self.getDBConn(),
                                 transaction = self.existingTransaction())

    def _insertWMTask(self, wmSpecName, task):
        """
        """
        taskAction = self.daofactory(classname = "WMSpec.AddTask")

        taskAction.execute(wmSpecName, task.name(), task.dbsUrl(),
                           conn = self.getDBConn(),
                           transaction = self.existingTransaction())

    def _insertWhiteList(self, elementID, whitelist):
        """
        """
        self._insertSite(whitelist)

        whitelistAction = self.daofactory(classname = "Site.AddWhiteList")
        whitelistAction.execute(elementID, whitelist, conn = self.getDBConn(),
                             transaction = self.existingTransaction())

    def _insertBlackList(self, elementID, blacklist):
        """
        """
        self._insertSite(blacklist)
        blacklistAction = self.daofactory(classname = "Site.AddBlackList")
        blacklistAction.execute(elementID, blacklist, conn = self.getDBConn(),
                             transaction = self.existingTransaction())


    def _insertInputs(self, primary, parents):
        """
        Insert blocks and record parentage info
        """
        def _inputCreation(data):
            """
            Internal function to insert an input
            """
            dataAction.execute(data, conn = self.getDBConn(),
                               transaction = self.existingTransaction())

        dataAction = self.daofactory(classname = "Data.New")
        dataParentageAct = self.daofactory(classname = "Data.AddParent")

        _inputCreation(primary)
        for parent in parents:
            _inputCreation(parent)
            dataParentageAct.execute(primary,
                                      parent,
                                      conn = self.getDBConn(),
                                      transaction = self.existingTransaction())

    def _insertSite(self, sites):
        """
        Insert site into database
        """
        siteAction = self.daofactory(classname = "Site.New")
        siteAction.execute(sites,
                           conn = self.getDBConn(),
                           transaction = self.existingTransaction())

    def _match(self, conditions):
        """
        Match resources to available work
        """
        matchAction = self.daofactory(classname = "WorkQueueElement.GetWork")
        elements, unmatched = matchAction.execute(conditions, self.params['ItemWeight'],
                                       conn = self.getDBConn(),
                                       transaction = self.existingTransaction())
        return elements, unmatched


    def _getLocations(self, dataNames, fullRefresh):
        """
        Return mapping of item to location as given by phedex
        """

        args = {}
        if self.params['TrackLocationOrSubscription'] == 'subscription':
            return self._getPhEDExSubscriptions(dataNames, fullRefresh)
        elif self.params['TrackLocationOrSubscription'] == 'location':
            args['block'] = dataNames
            if not self.params['ReleaseIncompleteBlocks']:
                args['complete'] = 'y'
            if not self.params['ReleaseRequireSubscribed']:
                args['subscribed'] = 'y'
            if not fullRefresh:
                args['update_since'] = self.lastLocationUpdate
            response = self.phedexService.getReplicaInfoForBlocks(**args)['phedex']
            self.lastLocationUpdate = response['request_timestamp']
            #result = defaultdict(list)
            #[ result[block['name']].append(ses['se']) for ses in block['replica'] for block in response['block'] ]
            result = {}
            for block in response['block']:
                result.setdefault(block['name'], [])
                result[block['name']].extend([se['se'] for se in block['replica']])
            return result
        else:
            raise RuntimeError, "invalid selection"


    def _getPhEDExSubscriptions(self, dataNames, fullRefresh):
        """
        Return mapping of block/dataset to subscribed locations
        """
        args = {}
        args['suspended'] = 'n' # require subscription to be active
        if not fullRefresh:
            args['update_since'] = self.lastLocationUpdate
        args['block'], args['dataset'] = [], []
        for item in dataNames:
            if item.find('#') != -1:
                args['block'].append(item)
            else:
                args['dataset'].append(item)

        response = self.phedexService.subscriptions(**args)['phedex']
        self.lastLocationUpdate = response['request_timestamp']
        result = {}

        # iterate over response as can't jump to specific datasets
        for dset in response['dataset']:

            if dset['name'] in args['dataset']:

                # we have work for the dataset
                if dset['subscription']:
                    # dataset level subscription
                    result[dset['name']] = [x['node'] for x in dset['subscription']]
                else:
                    # block level subscription
                    # Create dataset level subscription from ensemble
                    # of block level subscriptions
                    #TODO: Does this check all blocks not just those updated
                    commonSites = set()
                    for block in dset['block']:
                        commonSites = commonSites & set([x['node'] for x in block['subscription']])
                    result[dset['name']] = list(commonSites)

            else:
                # have work for some blocks in this dataset
                if dset.has_key('subscription'):
                    # work for block and have dataset level subscription
                    subs = [x['node'] for x in dset['subscription']]
                    for block in dset['block']:
                        if block['name'] in args['block']:
                            result[block['name']] = [x['node'] for x in block['subscription']]
                else:
                    # block level subscriptions
                    for block in dset['block']:
                        # record blocks we have work for
                        if block['name'] in args['block']:
                            result[block['name']] = [x['node'] for x in block['subscription']]

        return result

    def _get_remote_queue(self, queue):
        """
        Get an object to talk to a remote queue
        """
        # tests generally get the queue object passed in direct
        if isinstance(queue, WorkQueue):
            return queue
        try:
            return self.remote_queues[queue]
        except KeyError:
            #TODO: instantiate REST connector here, add to dict
            pass
