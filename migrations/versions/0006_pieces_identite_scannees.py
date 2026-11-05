"""pièces d'identité scannées (MRZ / OCR) : expiration, scans recto-verso, journal des extractions

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05 22:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TABLE … ADD COLUMN natif (sans recréation de la table) : l'index unique partiel
    # « uq_guests_identity » (WHERE id_number_norm != '') reste intact.
    op.add_column('guests', sa.Column('id_expiry_date', sa.Date(), nullable=True))
    op.add_column('guests', sa.Column('id_scan_path', sa.String(length=255), nullable=False, server_default=''))
    op.add_column('guests', sa.Column('id_scan_back_path', sa.String(length=255), nullable=False, server_default=''))
    op.add_column('guests', sa.Column('id_scanned_at', sa.DateTime(), nullable=True))

    op.create_table(
        'id_scans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guest_id', sa.Integer(), nullable=True),
        sa.Column('reservation_id', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=60), nullable=False),
        sa.Column('engine', sa.String(length=20), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('raw_mrz', sa.Text(), nullable=False),
        sa.Column('fields_json', sa.Text(), nullable=False),
        sa.Column('checksums_valid', sa.Boolean(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['guest_id'], ['guests.id'], name=op.f('fk_id_scans_guest_id_guests'),
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reservation_id'], ['reservations.id'],
                                name=op.f('fk_id_scans_reservation_id_reservations'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_id_scans_user_id_users'),
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_id_scans')),
    )
    with op.batch_alter_table('id_scans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_id_scans_guest_id'), ['guest_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_id_scans_reservation_id'), ['reservation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_id_scans_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_id_scans_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('id_scans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_id_scans_created_at'))
        batch_op.drop_index(batch_op.f('ix_id_scans_status'))
        batch_op.drop_index(batch_op.f('ix_id_scans_reservation_id'))
        batch_op.drop_index(batch_op.f('ix_id_scans_guest_id'))
    op.drop_table('id_scans')
    # SQLite ≥ 3.35 : ALTER TABLE … DROP COLUMN natif (l'index partiel des pièces d'identité est conservé).
    op.drop_column('guests', 'id_scanned_at')
    op.drop_column('guests', 'id_scan_back_path')
    op.drop_column('guests', 'id_scan_path')
    op.drop_column('guests', 'id_expiry_date')
