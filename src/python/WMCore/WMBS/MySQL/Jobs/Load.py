#!/usr/bin/env python
"""
_Load_
MySQL implementation of Jobs.Load
"""
__all__ = []
__revision__ = "$Id: Load.py,v 1.2 2008/08/09 22:22:55 metson Exp $"
__version__ = "$Revision: 1.2 $"

from WMCore.WMBS.MySQL.Base import MySQLBase

class Load(MySQLBase):
    sql = "select file from wmbs_job_assoc where job = :jobid"
    
    def format(self, result):
        out = []
        for r in result:
            for i in r.fetchall():
                out.append(i[0])
        return out
               
    def execute(self, id=0, conn = None, transaction = False):
        binds = self.getBinds(jobid = id)
        result = self.dbi.processData(self.sql, binds)
        
        return self.format(result)