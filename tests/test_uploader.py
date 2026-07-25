import unittest
from unittest.mock import MagicMock
import discord
import config
from cogs.uploader import can_upload


class TestUploaderPermissions(unittest.TestCase):
    def setUp(self):
        self.orig_admin = config.ADMIN_USER_IDS
        self.orig_users = config.UPLOADER_USER_IDS
        self.orig_roles = config.UPLOADER_ROLE_IDS

    def tearDown(self):
        config.ADMIN_USER_IDS = self.orig_admin
        config.UPLOADER_USER_IDS = self.orig_users
        config.UPLOADER_ROLE_IDS = self.orig_roles

    def test_can_upload_superadmin(self):
        config.ADMIN_USER_IDS = [100]
        config.UPLOADER_USER_IDS = []
        config.UPLOADER_ROLE_IDS = []
        interaction = MagicMock()
        interaction.user.id = 100
        self.assertTrue(can_upload(interaction))

    def test_can_upload_uploader_user_id(self):
        config.ADMIN_USER_IDS = [100]
        config.UPLOADER_USER_IDS = [200]
        config.UPLOADER_ROLE_IDS = []
        interaction = MagicMock()
        interaction.user.id = 200
        self.assertTrue(can_upload(interaction))

    def test_can_upload_uploader_role_id(self):
        config.ADMIN_USER_IDS = [100]
        config.UPLOADER_USER_IDS = []
        config.UPLOADER_ROLE_IDS = [999]
        
        interaction = MagicMock()
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 500
        role = MagicMock()
        role.id = 999
        interaction.user.roles = [role]
        self.assertTrue(can_upload(interaction))

    def test_cannot_upload_unauthorized(self):
        config.ADMIN_USER_IDS = [100]
        config.UPLOADER_USER_IDS = [200]
        config.UPLOADER_ROLE_IDS = [999]
        
        interaction = MagicMock()
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 500
        role = MagicMock()
        role.id = 111
        interaction.user.roles = [role]
        self.assertFalse(can_upload(interaction))


if __name__ == "__main__":
    unittest.main()
