# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from openerp import api, SUPERUSER_ID
from openerp.osv import fields, osv

from werkzeug.local import Local, release_local

import os
import logging
import boto3
from botocore.exceptions import ClientError


_logger = logging.getLogger(__name__)


S3_BUCKET = 'S3_BUCKET'
AWS_ACCESS_KEY_ID = 'AWS_ACCESS_KEY_ID'
AWS_SECRET_ACCESS_KEY = 'AWS_SECRET_ACCESS_KEY'

class S3Error(Exception):
    pass


class S3NoSuchKey(S3Error):
    pass


class ir_attachment(osv.osv):

    _inherit = 'ir.attachment'
    _local = Local()

    @property
    def _s3_enabled(self):
        return all(
            bool(os.environ.get(key, None))
            for key in
            (S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
        )

    @property
    def _s3_bucket(self):
        return os.environ.get(S3_BUCKET)

    @property
    def _s3_client(self):
        if not hasattr(self._local, 's3_client'):
            self._local.s3_client = boto3.client(
                's3',
                aws_access_key_id=os.environ.get(AWS_ACCESS_KEY_ID),
                aws_secret_access_key=os.environ.get(AWS_SECRET_ACCESS_KEY)
            )

        return self._local.s3_client

    def create(self, cr, uid, values, context=None):
        res = super(ir_attachment, self).create(cr, uid, values, context)
        print res, values.get('datas_fname')
        return res

    def _data_get(self, cr, uid, ids, name, arg, context=None):
        if context is None:
            context = {}
        result = {}
        bin_size = context.get('bin_size')
        print "DATA GET ", ids
        for attach in self.browse(cr, uid, ids, context=context):
            if attach.store_fname:
                try:
                    result[attach.id] = self._file_read(cr, uid, attach.store_fname, bin_size, attach.s3_exists)
                except S3NoSuchKey:
                    # SUPERUSER_ID as probably don't have write access, trigger during create
                    _logger.warning("Preventing further s3 (%s) lookups for '%s'", self._s3_bucket, attach.store_fname)
                    self.write(cr, SUPERUSER_ID, [attach.id], { 's3_exists': False }, context=context)
                    result[attach.id] = ''
                except S3Error:
                    result[attach.id] = ''
            else:
                result[attach.id] = attach.db_datas
        return result

    def _data_set(self, cr, uid, id, name, value, arg, context=None):
        res = super(ir_attachment, self)._data_set(cr, uid, id, name, value, arg, context=None)
        print "DATA SET", id
        if self._s3_enabled:
            attach = self.browse(cr, uid, id, context=context)
            s3_exists = True
            try:
                self._s3_put(cr, uid, attach.store_fname)
            except S3Error:
                s3_exists = False
            self.write(cr, SUPERUSER_ID, [id], { 's3_exists': s3_exists }, context=context)
        else:
            _logger.warning("S3 is not enabled, dataloss for attachment [%s] is imminent", id)
        return res

    def _file_read(self, cr, uid, fname, bin_size=False, s3_exists=True):
        full_path = self._full_path(cr, uid, fname)
        if not os.path.exists(full_path) and self._s3_enabled and s3_exists:
            self._s3_get(cr, uid, fname)
        return super(ir_attachment, self)._file_read(cr, uid, fname, bin_size=bin_size)

    def _file_delete(self, cr, uid, fname):
        if self._s3_enabled:
            try:
                _logger.info("S3 (%s) delete '%s'", self._s3_bucket, fname)
                self._s3_client.delete_object(Bucket=self._s3_bucket, Key=fname)
            except ClientError:
                pass
        return super(ir_attachment, self)._file_delete(cr, uid, fname)

    def _s3_get(self, cr, uid, fname):
        try:
            _logger.info("S3 (%s) get '%s'", self._s3_bucket, fname)
            r = self._s3_client.get_object(Bucket=self._s3_bucket, Key=fname)
        except ClientError as e:
            _logger.warning("S3 (%s) get '%s'", self._s3_bucket, fname, exc_info=True)
            if e.response['Error']['Code'] == "NoSuchKey":
                raise S3NoSuchKey
            raise S3Error

        bin_data = r['Body'].read()
        checksum = self._compute_checksum(bin_data)
        value = bin_data.encode('base64')
        super(ir_attachment, self)._file_write(cr, uid, value, checksum)

    def _s3_put(self, cr, uid, fname):
        value = super(ir_attachment, self)._file_read(cr, uid, fname)
        bin_data = value.decode('base64')
        try:
            _logger.info("S3 (%s) put '%s'", self._s3_bucket, fname)
            self._s3_client.put_object(Bucket=self._s3_bucket, Key=fname, Body=bin_data)
        except ClientError:
            _logger.warning("S3 (%s) put '%s'", self._s3_bucket, fname, exc_info=True)
            raise S3Error

    _columns = {
        's3_exists': fields.boolean(string='Exists in s3 bucket'),
        'datas': fields.function(_data_get, fnct_inv=_data_set, string='File Content', type="binary", nodrop=True),
    }

    _defaults = {
        's3_exists': True,
    }

    @api.multi
    def action_s3_sync(self):
        for attachment in self:
            exists = attachment.s3_exists
            if exists:
                try:
                    attachment._s3_get(attachment.store_fname)
                    continue
                except S3NoSuchKey:
                    exists = False

            try:
                attachment._s3_put(attachment.store_fname)
                exists = True
            except Exception:
                pass

            attachment.write({ 's3_exists': exists })
