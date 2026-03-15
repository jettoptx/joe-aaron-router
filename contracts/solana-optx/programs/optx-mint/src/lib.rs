use anchor_lang::prelude::*;
use anchor_spl::token::{self, Mint, MintTo, Token, TokenAccount};

declare_id!("OPTXMint111111111111111111111111111111");

/// OPTX SPL Token Mint Program
///
/// Receives Wormhole VAA from XRPL Xahau Hook and mints OPTX tokens
/// after verifying gaze attestation hash.
///
/// Flow: Xahau Hook → Wormhole VAA → This program → SPL OPTX mint
///
/// Deploy: anchor build && anchor deploy --provider.cluster devnet
/// RPC: https://devnet.helius-rpc.com/?api-key=98ca6456-20a8-4518-8393-1b9ee6c2b7f3

#[program]
pub mod optx_mint {
    use super::*;

    /// Initialize the OPTX mint authority PDA
    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.authority = ctx.accounts.authority.key();
        config.total_mints = 0;
        config.xrpl_emitter = [0u8; 20]; // Set during setup
        config.bump = ctx.bumps.config;
        msg!("OPTX Mint initialized. Authority: {}", config.authority);
        Ok(())
    }

    /// Set the XRPL hook emitter account (one-time setup)
    pub fn set_emitter(
        ctx: Context<AdminAction>,
        emitter: [u8; 20],
    ) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.xrpl_emitter = emitter;
        msg!("XRPL emitter set.");
        Ok(())
    }

    /// Verify Wormhole VAA from XRPL and mint OPTX tokens
    ///
    /// # Arguments
    /// * `vaa_data` - Raw Wormhole VAA bytes from XRPL guardian network
    /// * `gaze_hash` - 32-byte SHA256 of AARON gaze attestation
    /// * `optx_amount` - Number of OPTX tokens to mint (from Xahau Hook state)
    /// * `xrpl_sender` - 20-byte XRPL account that sent the payment
    pub fn verify_and_mint(
        ctx: Context<VerifyAndMint>,
        vaa_data: Vec<u8>,
        gaze_hash: [u8; 32],
        optx_amount: u64,
        xrpl_sender: [u8; 20],
    ) -> Result<()> {
        let config = &ctx.accounts.config;

        // 1. Verify VAA payload matches expected format
        // TODO: Full Wormhole VAA verification via wormhole-anchor-sdk
        // For devnet: validate payload structure
        require!(vaa_data.len() >= 68, ErrorCode::InvalidVAA);

        // Extract gaze hash from VAA payload (bytes 0..32)
        let vaa_gaze_hash: [u8; 32] = vaa_data[0..32]
            .try_into()
            .map_err(|_| ErrorCode::InvalidVAA)?;

        // Extract sender from VAA payload (bytes 32..52)
        let vaa_sender: [u8; 20] = vaa_data[32..52]
            .try_into()
            .map_err(|_| ErrorCode::InvalidVAA)?;

        // Extract amount from VAA payload (bytes 52..60)
        let vaa_amount = u64::from_be_bytes(
            vaa_data[52..60]
                .try_into()
                .map_err(|_| ErrorCode::InvalidVAA)?,
        );

        // 2. Verify consistency
        require!(vaa_gaze_hash == gaze_hash, ErrorCode::GazeMismatch);
        require!(vaa_sender == xrpl_sender, ErrorCode::SenderMismatch);
        require!(vaa_amount == optx_amount, ErrorCode::AmountMismatch);
        require!(optx_amount > 0, ErrorCode::ZeroMint);

        // 3. Verify emitter matches configured XRPL hook
        require!(
            vaa_sender == config.xrpl_emitter || config.xrpl_emitter == [0u8; 20],
            ErrorCode::InvalidEmitter
        );

        // 4. Check attestation hasn't been used (replay protection)
        let attestation = &mut ctx.accounts.attestation;
        require!(!attestation.used, ErrorCode::ReplayAttack);
        attestation.used = true;
        attestation.gaze_hash = gaze_hash;
        attestation.optx_amount = optx_amount;
        attestation.xrpl_sender = xrpl_sender;
        attestation.sol_recipient = ctx.accounts.recipient.key();
        attestation.timestamp = Clock::get()?.unix_timestamp;

        // 5. Mint OPTX SPL tokens
        let seeds = &[b"optx_config".as_ref(), &[config.bump]];
        let signer = &[&seeds[..]];

        let cpi_accounts = MintTo {
            mint: ctx.accounts.optx_mint.to_account_info(),
            to: ctx.accounts.recipient_token.to_account_info(),
            authority: ctx.accounts.config.to_account_info(),
        };
        let cpi_ctx = CpiContext::new_with_signer(
            ctx.accounts.token_program.to_account_info(),
            cpi_accounts,
            signer,
        );

        // Mint with 6 decimals (1 OPTX = 1_000_000 units)
        let mint_amount = optx_amount
            .checked_mul(1_000_000)
            .ok_or(ErrorCode::Overflow)?;
        token::mint_to(cpi_ctx, mint_amount)?;

        // 6. Update global mint counter
        let config = &mut ctx.accounts.config;
        config.total_mints = config
            .total_mints
            .checked_add(optx_amount)
            .ok_or(ErrorCode::Overflow)?;

        msg!(
            "OPTX Minted: {} tokens to {}. Gaze verified. Total: {}",
            optx_amount,
            ctx.accounts.recipient.key(),
            config.total_mints
        );

        Ok(())
    }
}

/* ─── Accounts ─── */

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(
        init,
        payer = authority,
        space = 8 + OPTXConfig::INIT_SPACE,
        seeds = [b"optx_config"],
        bump
    )]
    pub config: Account<'info, OPTXConfig>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct AdminAction<'info> {
    #[account(
        mut,
        seeds = [b"optx_config"],
        bump = config.bump,
        has_one = authority
    )]
    pub config: Account<'info, OPTXConfig>,
    pub authority: Signer<'info>,
}

#[derive(Accounts)]
#[instruction(vaa_data: Vec<u8>, gaze_hash: [u8; 32])]
pub struct VerifyAndMint<'info> {
    #[account(
        mut,
        seeds = [b"optx_config"],
        bump = config.bump
    )]
    pub config: Account<'info, OPTXConfig>,

    #[account(
        init,
        payer = payer,
        space = 8 + Attestation::INIT_SPACE,
        seeds = [b"attestation", gaze_hash.as_ref()],
        bump
    )]
    pub attestation: Account<'info, Attestation>,

    #[account(mut)]
    pub optx_mint: Account<'info, Mint>,

    #[account(mut)]
    pub recipient_token: Account<'info, TokenAccount>,

    /// CHECK: Recipient wallet (validated by token account owner)
    pub recipient: UncheckedAccount<'info>,

    #[account(mut)]
    pub payer: Signer<'info>,

    pub token_program: Program<'info, Token>,
    pub system_program: Program<'info, System>,
}

/* ─── State ─── */

#[account]
#[derive(InitSpace)]
pub struct OPTXConfig {
    pub authority: Pubkey,       // Admin authority
    pub total_mints: u64,        // Cumulative OPTX minted
    pub xrpl_emitter: [u8; 20], // XRPL hook account (Xahau)
    pub bump: u8,
}

#[account]
#[derive(InitSpace)]
pub struct Attestation {
    pub used: bool,              // Replay protection
    pub gaze_hash: [u8; 32],    // AARON gaze attestation hash
    pub optx_amount: u64,        // Tokens minted
    pub xrpl_sender: [u8; 20],  // XRPL source account
    pub sol_recipient: Pubkey,   // Solana recipient
    pub timestamp: i64,          // Unix timestamp
}

/* ─── Errors ─── */

#[error_code]
pub enum ErrorCode {
    #[msg("Invalid Wormhole VAA format")]
    InvalidVAA,
    #[msg("Gaze attestation hash mismatch")]
    GazeMismatch,
    #[msg("XRPL sender mismatch")]
    SenderMismatch,
    #[msg("OPTX amount mismatch")]
    AmountMismatch,
    #[msg("Zero mint amount")]
    ZeroMint,
    #[msg("Invalid XRPL emitter")]
    InvalidEmitter,
    #[msg("Attestation already used (replay attack)")]
    ReplayAttack,
    #[msg("Arithmetic overflow")]
    Overflow,
}
