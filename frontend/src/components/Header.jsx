import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getWishlist } from '../lib/demo.js'

/**
 * Shared top nav for every page -- was previously copy-pasted between
 * Landing and SaleDetail with slightly different contents each time.
 *
 * The search bar and cart icon are visual only (this storefront has no
 * account system or cart -- Buy Now checks out a single unit immediately,
 * which is the point of a flash-sale drop), but a real ecommerce header has
 * them, and a demo that's missing them reads as a prototype rather than a
 * shop. The wishlist heart is the one exception -- it's a real, if purely
 * client-side (localStorage), toggle; see WishlistButton in bits.jsx and
 * the wishlist helpers in lib/demo.js.
 */
export default function Header({ active }) {
  const linkClass = (key) =>
    `transition ${active === key ? 'text-white' : 'text-white/70 hover:text-white'}`

  const [wishlistCount, setWishlistCount] = useState(0)
  useEffect(() => {
    const sync = () => setWishlistCount(getWishlist().length)
    sync()
    window.addEventListener('wishlist-change', sync)
    return () => window.removeEventListener('wishlist-change', sync)
  }, [])

  return (
    <header className="sticky top-0 z-30 border-b border-neutral-800 bg-neutral-900">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-5 px-4">
        <Link to="/" className="shrink-0 text-lg font-black uppercase tracking-tight text-white">
          Gadget<span className="text-orange-500">Z</span>
        </Link>

        <nav className="hidden gap-5 text-[11px] font-bold uppercase tracking-[0.12em] sm:flex">
          <Link to="/" className={linkClass('shop')}>
            Flash Sales
          </Link>
          <Link to="/engines" className={linkClass('engines')}>
            Engine Room
          </Link>
          <span className="cursor-default text-white/40">Deals</span>
        </nav>

        {/* Visual-only search -- see the component docstring. */}
        <div className="mx-auto hidden max-w-sm flex-1 items-center gap-2 rounded-full bg-white/10 px-3.5 py-1.5 text-xs text-white/50 sm:flex">
          <SearchGlyph />
          <span className="truncate">Search sneakers, gadgets, drops…</span>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span className="hidden items-center gap-2 text-[11px] font-bold uppercase tracking-[0.12em] text-white/70 xs:flex">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            Live
          </span>
          <IconButton label="Wishlist" badge={wishlistCount}>
            <HeartGlyph filled={wishlistCount > 0} />
          </IconButton>
          <IconButton label="Cart">
            <BagGlyph />
          </IconButton>
        </div>
      </div>
    </header>
  )
}

function IconButton({ label, children, badge }) {
  return (
    <button
      type="button"
      title={label}
      className="relative grid h-8 w-8 shrink-0 place-items-center rounded-full text-white/80 transition hover:bg-white/10 hover:text-white"
    >
      {children}
      {badge > 0 && (
        <span className="absolute -right-1 -top-1 grid h-4 min-w-[16px] place-items-center rounded-full bg-orange-500 px-1 text-[9px] font-black text-white">
          {badge}
        </span>
      )}
    </button>
  )
}

function SearchGlyph() {
  return (
    <svg viewBox="0 0 20 20" className="h-3.5 w-3.5 shrink-0 fill-none stroke-current" strokeWidth="2">
      <circle cx="9" cy="9" r="6" />
      <path d="M17 17l-4-4" strokeLinecap="round" />
    </svg>
  )
}

function HeartGlyph({ filled }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`h-4 w-4 stroke-current transition ${filled ? 'fill-orange-500' : 'fill-none'}`}
      strokeWidth="1.8"
    >
      <path
        d="M10 17s-6.5-4.02-6.5-8.5A3.75 3.75 0 0110 6a3.75 3.75 0 016.5 2.5C16.5 12.98 10 17 10 17z"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function BagGlyph() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 fill-none stroke-current" strokeWidth="1.8">
      <path d="M5 7h10l-.8 9.2a1 1 0 01-1 .8H6.8a1 1 0 01-1-.8L5 7z" strokeLinejoin="round" />
      <path d="M7.5 7V5.5a2.5 2.5 0 015 0V7" strokeLinecap="round" />
    </svg>
  )
}
